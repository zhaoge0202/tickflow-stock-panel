"""竞价秒级回放服务。

用于复盘「盘后候选 + 09:23-09:25 最近竞价快照 + 09:25-09:30
开盘 trade 快照确认」。逐秒帧会沿用最近已知快照, 并显式输出源事件时间和
stale 秒数; 不会把没有原始事件的秒伪装成真实新 tick。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.market_time import cn_today
from app.services import quote_tick_store, strategy_cache

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
AUCTION_START = dt_time(9, 23)
AUCTION_END = dt_time(9, 25)
TRADE_END = dt_time(9, 30)
MAX_DEFAULT_FRAMES = 600
MAX_DYNAMIC_FRAMES = 300
QUOTE_WINDOW_FINGERPRINT_TTL = 5.0
DEFAULT_DYNAMIC_STRATEGY_IDS = (
    "custom_dual_edge",
    "custom_dual_edge_v3",
    "custom_dual_edge_focus",
)

_dynamic_history_lock = threading.Lock()
_dynamic_history_cache: dict[tuple, object] = {}
_quote_window_lock = threading.Lock()
_quote_window_cache: dict[tuple[str, str], dict] = {}


def replay_cached_strategy_results(
    data_dir: Path,
    cached: dict | None,
    *,
    as_of: date | None = None,
    trade_date: date | None = None,
    strategy_ids: list[str] | None = None,
    as_of_ts: int | None = None,
    include_frames: bool = True,
    include_candidates: bool = False,
    max_frames: int = MAX_DEFAULT_FRAMES,
) -> dict:
    """按真实 quote_ticks 回放竞价确认过程。

    as_of_ts 传入时只返回该时刻的 frame; 不传时返回 09:23-09:30 的逐秒帧。
    没有新 tick 的秒只延续上一条源快照, 并带 stale_seconds。
    """
    cached = cached or {}
    now = datetime.now(tz=CN_TZ)
    cached_as_of = _parse_date(cached.get("as_of")) if cached.get("as_of") else None
    signal_date = as_of or cached_as_of
    trade_day = trade_date or cn_today()
    cache_results = cached.get("results") or {}

    if signal_date is None:
        return _empty_payload(
            status="no_cache",
            signal_date=None,
            trade_day=trade_day,
            now=now,
        )

    requested_ids = _normalize_strategy_ids(strategy_ids)
    if requested_ids is None:
        requested_ids = [
            sid for sid, result in cache_results.items()
            if isinstance(result, dict)
        ]

    if cached_as_of is not None and cached_as_of != signal_date:
        return _stale_payload(
            signal_date=signal_date,
            cached_as_of=cached_as_of,
            trade_day=trade_day,
            requested_ids=requested_ids,
            now=now,
        )

    base_results, all_symbols = _base_results(
        cache_results,
        requested_ids,
        signal_date=signal_date,
        trade_day=trade_day,
    )
    if not requested_ids:
        return _empty_payload(
            status="empty_candidates",
            signal_date=signal_date,
            trade_day=trade_day,
            now=now,
        )

    rows = _load_rows(data_dir, trade_day, all_symbols)
    classified = _classify_rows(rows, trade_day)
    timeline = _timeline(classified, trade_day)
    quality = _quality(rows, classified, all_symbols)
    max_frames = max(1, min(int(max_frames or MAX_DEFAULT_FRAMES), MAX_DEFAULT_FRAMES))

    if as_of_ts is not None:
        frame = _build_frame(
            int(as_of_ts),
            base_results,
            classified,
            include_candidates=include_candidates,
        )
        return {
            **_payload_base(_status_from_quality(quality), signal_date, trade_day, now),
            "timeline": timeline,
            "frame": frame,
            "frames": [],
            "final_frame": frame,
            "data_quality": quality,
            "frame_mode": "single_as_of",
        }

    frames = []
    if include_frames:
        for point in timeline[:max_frames]:
            frames.append(
                _build_frame(
                    int(point["as_of_ts"]),
                    base_results,
                    classified,
                    include_candidates=include_candidates,
                    point=point,
                )
            )
    final_frame = (
        _build_frame(
            int(timeline[-1]["as_of_ts"]),
            base_results,
            classified,
            include_candidates=include_candidates,
            point=timeline[-1],
        )
        if timeline
        else _build_frame(
            _window_start_ms(trade_day, TRADE_END) - 1,
            base_results,
            classified,
            include_candidates=include_candidates,
        )
    )
    status = _status_from_quality(quality)
    return {
        **_payload_base(status, signal_date, trade_day, now),
        "timeline": timeline,
        "frames": frames,
        "frame": None,
        "final_frame": final_frame,
        "data_quality": quality,
        "frame_mode": "dense_seconds",
        "frames_truncated": include_frames and len(timeline) > max_frames,
        "max_frames": max_frames,
    }


def replay_dynamic_strategy_results(
    repo,
    engine,
    *,
    as_of: date | None = None,
    trade_date: date | None = None,
    strategy_ids: list[str] | None = None,
    as_of_ts: int | None = None,
    include_frames: bool = False,
    include_candidates: bool = True,
    max_frames: int = MAX_DYNAMIC_FRAMES,
    asset_type: str = "stock",
    timeframe: str = "1d",
) -> dict:
    """09:25 后按真实竞价/开盘快照逐秒重跑策略。

    与 replay_cached_strategy_results 不同, 这里不读取也不写入策略缓存。
    as_of 是盘后基准日, trade_date 是用 quote_ticks 生成动态 bar 的交易日。
    """
    import polars as pl

    from app.services.screener import ScreenerService
    from app.strategy import config as strategy_config

    now = datetime.now(tz=CN_TZ)
    data_dir = Path(repo.store.data_dir)
    trade_day = trade_date or cn_today()
    svc = ScreenerService(repo, asset_type=asset_type)
    signal_date = as_of or svc.latest_date()
    if signal_date is None:
        return _empty_dynamic_payload(
            status="no_history",
            signal_date=None,
            trade_day=trade_day,
            now=now,
        )

    requested_ids = _normalize_strategy_ids(strategy_ids)
    if requested_ids is None:
        requested_ids = [sid for sid in DEFAULT_DYNAMIC_STRATEGY_IDS if engine.has(sid)]
    if not requested_ids:
        return _empty_dynamic_payload(
            status="empty_strategies",
            signal_date=signal_date,
            trade_day=trade_day,
            now=now,
        )

    all_overrides = strategy_config.list_overrides(data_dir)
    overrides_map = {sid: all_overrides.get(sid, {}) for sid in requested_ids}
    params_map = {
        sid: dict((overrides_map.get(sid) or {}).get("params") or {})
        for sid in requested_ids
    }

    if as_of_ts is not None:
        rows, classified, quality = _load_dynamic_asof_inputs(
            data_dir,
            trade_day,
            as_of_ts=int(as_of_ts),
        )
    else:
        rows = _load_dynamic_rows(data_dir, trade_day, as_of_ts=None)
        classified = _classify_rows(rows, trade_day)
        quality = _quality(rows, classified, set())
    timeline = [] if as_of_ts is not None else _dynamic_timeline(classified, trade_day)
    max_frames = max(1, min(int(max_frames or MAX_DYNAMIC_FRAMES), MAX_DYNAMIC_FRAMES))

    try:
        history = _load_dynamic_history(
            svc,
            engine,
            signal_date,
            requested_ids,
            params_map=params_map,
            overrides_map=overrides_map,
        )
    except Exception as exc:
        logger.warning("auction dynamic history load failed: %s", exc)
        history = pl.DataFrame()
    if history.is_empty():
        payload = _empty_dynamic_payload(
            status="no_history",
            signal_date=signal_date,
            trade_day=trade_day,
            now=now,
        )
        payload.update({"timeline": timeline, "data_quality": quality})
        return payload

    if as_of_ts is not None:
        frame = _build_dynamic_frame(
            int(as_of_ts),
            repo,
            engine,
            history,
            signal_date=signal_date,
            trade_day=trade_day,
            strategy_ids=requested_ids,
            params_map=params_map,
            overrides_map=overrides_map,
            classified=classified,
            include_candidates=include_candidates,
            asset_type=asset_type,
            timeframe=timeframe,
        )
        payload = {
            **_dynamic_payload_base(_dynamic_status(frame, quality), signal_date, trade_day, now),
            "timeline": timeline,
            "frame": frame,
            "frames": [],
            "final_frame": frame,
            "data_quality": _dynamic_quality(quality, frame),
            "frame_mode": "single_as_of",
        }
        _record_dynamic_history(
            data_dir,
            engine=engine,
            signal_date=signal_date,
            trade_day=trade_day,
            final_frame=frame,
            classified=classified,
            final_ts=int(as_of_ts),
            requested_ids=requested_ids,
            params_map=params_map,
        )
        return payload

    frames = []
    if include_frames:
        for point in timeline[:max_frames]:
            frames.append(
                _build_dynamic_frame(
                    int(point["as_of_ts"]),
                    repo,
                    engine,
                    history,
                    signal_date=signal_date,
                    trade_day=trade_day,
                    strategy_ids=requested_ids,
                    params_map=params_map,
                    overrides_map=overrides_map,
                    classified=classified,
                    include_candidates=include_candidates,
                    asset_type=asset_type,
                    timeframe=timeframe,
                    point=point,
                )
            )

    final_point = timeline[-1] if timeline else None
    final_ts = (
        int(final_point["as_of_ts"])
        if final_point is not None
        else _window_start_ms(trade_day, TRADE_END) - 1
    )
    final_frame = _build_dynamic_frame(
        final_ts,
        repo,
        engine,
        history,
        signal_date=signal_date,
        trade_day=trade_day,
        strategy_ids=requested_ids,
        params_map=params_map,
        overrides_map=overrides_map,
        classified=classified,
        include_candidates=include_candidates,
        asset_type=asset_type,
        timeframe=timeframe,
        point=final_point,
    )
    payload = {
        **_dynamic_payload_base(_dynamic_status(final_frame, quality), signal_date, trade_day, now),
        "timeline": timeline,
        "frames": frames,
        "frame": None,
        "final_frame": final_frame,
        "data_quality": _dynamic_quality(quality, final_frame),
        "frame_mode": "dense_seconds",
        "frames_truncated": include_frames and len(timeline) > max_frames,
        "max_frames": max_frames,
    }
    _record_dynamic_history(
        data_dir,
        engine=engine,
        signal_date=signal_date,
        trade_day=trade_day,
        final_frame=final_frame,
        classified=classified,
        final_ts=final_ts,
        requested_ids=requested_ids,
        params_map=params_map,
    )
    return payload


def _record_dynamic_history(
    data_dir: Path,
    *,
    engine,
    signal_date: date,
    trade_day: date,
    final_frame: dict,
    classified: dict,
    final_ts: int,
    requested_ids: list[str],
    params_map: dict[str, dict],
) -> None:
    """把盘后候选与收盘前动态结果对齐，记录确认或淘汰原因。

    竞价窗口尚未结束时不写淘汰结论，避免股票在 09:25~09:30 的波动被过早
    固化。动态请求在确认窗口结束后，才把最终状态写入历史。
    """
    trade_end_ts = _window_start_ms(trade_day, TRADE_END)
    # 当前日的动态重算使用的是盘中实时 bar，不对应“前一交易日候选→次日确认”
    # 生命周期；只有 signal_date < trade_day 的跨日确认才落盘。
    if signal_date >= trade_day:
        return
    if final_ts < trade_end_ts - 1:
        return
    cached = strategy_cache.read_cache(data_dir)
    if not isinstance(cached, dict) or str(cached.get("as_of") or "") != signal_date.isoformat():
        return
    cache_results = cached.get("results") or {}
    if not isinstance(cache_results, dict):
        return

    from app.services import strategy_history

    auction_map = _latest_by_symbol(
        classified.get("auction_rows") or [],
        as_of_ts=min(final_ts, _window_start_ms(trade_day, AUCTION_END) - 1),
    )
    trade_map = _latest_by_symbol(
        classified.get("trade_rows") or [],
        as_of_ts=final_ts,
    )
    events: list[dict] = []
    selection_records: list[tuple[str, str, list[dict]]] = []
    for sid in requested_ids:
        raw = cache_results.get(sid)
        if not isinstance(raw, dict) or str(raw.get("as_of") or "") != signal_date.isoformat():
            continue
        base_rows = [row for row in raw.get("rows") or [] if isinstance(row, dict)]
        strategy = engine.get(sid) if hasattr(engine, "get") else None
        strategy_meta = getattr(strategy, "meta", {}) or {}
        strategy_name = strategy_meta.get("name") or sid
        selection_records.append((sid, strategy_name, base_rows))
        dynamic = (final_frame.get("results") or {}).get(sid) or {}
        candidate_symbols = {
            str(row.get("symbol") or "").strip().upper()
            for row in dynamic.get("dual_rows") or []
            if row.get("symbol")
        }
        confirmed_symbols = {
            str(row.get("symbol") or "").strip().upper()
            for row in dynamic.get("rows") or []
            if row.get("symbol")
        }
        for base_row in base_rows:
            symbol = str(base_row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            auction_row = auction_map.get(symbol)
            trade_row = trade_map.get(symbol)
            if symbol in confirmed_symbols:
                events.append(_outcome_event(
                    event_type="auction_confirmed",
                    status="confirmed",
                    strategy_id=sid,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    base_row=base_row,
                    signal_date=signal_date,
                    trade_day=trade_day,
                    reason_code="auction_confirmed",
                    reason="竞价和开盘动态确认通过",
                    auction_row=auction_row,
                    trade_row=trade_row,
                ))
                continue
            if symbol in candidate_symbols and (auction_row is None or trade_row is None):
                reason_code = (
                    "auction_data_incomplete"
                    if auction_row is None
                    else "open_snapshot_incomplete"
                )
                reason = "竞价或开盘快照不完整，暂未形成最终确认"
            else:
                reason_code, reason = _dynamic_rejection_reason(
                    sid,
                    base_row,
                    auction_row,
                    trade_row,
                    params_map.get(sid) or {},
                )
            events.append(_outcome_event(
                event_type="auction_rejected",
                status="rejected",
                strategy_id=sid,
                strategy_name=strategy_name,
                symbol=symbol,
                base_row=base_row,
                signal_date=signal_date,
                trade_day=trade_day,
                reason_code=reason_code,
                reason=reason,
                auction_row=auction_row,
                trade_row=trade_row,
            ))
    try:
        for sid, strategy_name, rows in selection_records:
            strategy_history.record_selection_snapshot(
                data_dir,
                strategy_id=sid,
                strategy_name=strategy_name,
                signal_date=signal_date.isoformat(),
                trade_date=trade_day.isoformat(),
                rows=rows,
            )
        strategy_history.record_auction_outcomes(data_dir, events)
    except Exception as exc:  # noqa: BLE001
        logger.warning("竞价结果历史记录失败: %s", exc)


def _outcome_event(
    *,
    event_type: str,
    status: str,
    strategy_id: str,
    strategy_name: str,
    symbol: str,
    base_row: dict,
    signal_date: date,
    trade_day: date,
    reason_code: str,
    reason: str,
    auction_row: dict | None,
    trade_row: dict | None,
) -> dict:
    auction_price = _snapshot_price(auction_row) if auction_row else None
    trade_price = _snapshot_price(trade_row) if trade_row else None
    observed = trade_row or auction_row or {}
    event_key = (
        f"auction:{event_type}:{strategy_id}:{symbol}:"
        f"{signal_date.isoformat()}:{trade_day.isoformat()}"
    )
    return {
        "event_key": event_key,
        "event_type": event_type,
        "status": status,
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "symbol": symbol,
        "name": base_row.get("name") or observed.get("name"),
        "signal_date": signal_date.isoformat(),
        "trade_date": trade_day.isoformat(),
        "phase": "open_confirm",
        "price": trade_price or auction_price,
        "change_pct": _float_or_none(observed.get("change_pct")),
        "score": base_row.get("score"),
        "signals": base_row.get("signals") or [],
        "reason_code": reason_code,
        "reason": reason,
        "metadata": {
            "base_price": base_row.get("close"),
            "base_change_pct": base_row.get("change_pct"),
            "auction_price": auction_price,
            "open_confirm_price": trade_price,
            "auction_event_ts": int((auction_row or {}).get("event_ts") or 0) or None,
            "open_confirm_event_ts": int((trade_row or {}).get("event_ts") or 0) or None,
        },
    }


def _dynamic_rejection_reason(
    strategy_id: str,
    base_row: dict,
    auction_row: dict | None,
    trade_row: dict | None,
    params: dict,
) -> tuple[str, str]:
    if auction_row is None:
        return "auction_data_missing", "未取得 09:25 前竞价快照，无法确认"
    if trade_row is None:
        return "open_snapshot_missing", "未取得 09:25~09:30 开盘快照，无法确认"
    base_price = _float_or_none(base_row.get("close"))
    auction_price = _snapshot_price(auction_row)
    trade_price = _snapshot_price(trade_row)
    if base_price in (None, 0) or auction_price is None or trade_price is None:
        return "dynamic_data_invalid", "竞价/开盘价格数据不完整，未形成确认"

    open_value = _positive_float(trade_row.get("open")) or auction_price
    open_gap = open_value / base_price - 1.0
    current_change = trade_price / base_price - 1.0
    if strategy_id in {"custom_dual_edge", "custom_dual_edge_focus", "custom_dual_edge_prime"}:
        default_gap_max = 4.0 if strategy_id == "custom_dual_edge_prime" else 3.5
        gap_min = float(params.get("gap_min", 2.0)) / 100.0
        gap_max = float(params.get("gap_max", default_gap_max)) / 100.0
        min_change = float(params.get("auction_min_change", 3.5)) / 100.0
        if open_gap < gap_min:
            return (
                "auction_gap_failed",
                f"竞价开盘 {open_gap * 100:+.2f}%，"
                f"低于最低高开 {gap_min * 100:.1f}%",
            )
        if open_gap > gap_max:
            return (
                "auction_gap_failed",
                f"竞价开盘 {open_gap * 100:+.2f}%，"
                f"超过最高高开 {gap_max * 100:.1f}%",
            )
        if current_change < min_change:
            return (
                "auction_strength_failed",
                f"开盘后涨跌 {current_change * 100:+.2f}%，"
                f"低于最低收涨 {min_change * 100:.1f}%",
            )
    return "dynamic_strategy_rejected", "竞价/开盘动态重算后未满足该策略条件"


def _empty_dynamic_payload(
    *,
    status: str,
    signal_date: date | None,
    trade_day: date,
    now: datetime,
) -> dict:
    return {
        "status": status,
        "mode": "auction_dynamic",
        "as_of": signal_date.isoformat() if signal_date else None,
        "trade_date": trade_day.isoformat(),
        "strategy_as_of": trade_day.isoformat(),
        "updated_at": _now_ms(now),
        "timeline": [],
        "frames": [],
        "frame": None,
        "final_frame": None,
        "data_quality": {},
        "timeline_sparse": False,
        "missing_seconds_are_carried_forward": True,
    }


def _dynamic_payload_base(status: str, signal_date: date, trade_day: date, now: datetime) -> dict:
    base = _payload_base(status, signal_date, trade_day, now)
    base.update({
        "mode": "auction_dynamic",
        "strategy_as_of": trade_day.isoformat(),
        "dynamic_recompute": True,
    })
    return base


def _load_dynamic_history(
    svc,
    engine,
    signal_date: date,
    strategy_ids: list[str],
    *,
    params_map: dict[str, dict],
    overrides_map: dict[str, dict],
):
    history_bars = engine.required_history_bars(
        strategy_ids,
        params_map=params_map,
        overrides_map=overrides_map,
    )
    cache_key = (
        str(Path(svc.repo.store.data_dir).resolve()),
        svc.asset_type,
        signal_date.isoformat(),
        tuple(strategy_ids),
        int(history_bars),
        _freeze_jsonish({sid: params_map.get(sid, {}) for sid in strategy_ids}),
        _freeze_jsonish({sid: overrides_map.get(sid, {}) for sid in strategy_ids}),
    )
    with _dynamic_history_lock:
        cached = _dynamic_history_cache.get(cache_key)
    if cached is not None:
        return cached

    if history_bars > 1:
        history = svc._load_enriched_history(signal_date, history_bars)
    else:
        history = svc._load_enriched_for_date(signal_date)
    if history is None or history.is_empty() or "date" not in history.columns:
        return history
    history = history.filter(history["date"] <= signal_date).sort(["symbol", "date"])
    with _dynamic_history_lock:
        _dynamic_history_cache[cache_key] = history
        if len(_dynamic_history_cache) > 8:
            oldest = next(iter(_dynamic_history_cache))
            _dynamic_history_cache.pop(oldest, None)
    return history


def _load_dynamic_rows(data_dir: Path, trade_day: date, *, as_of_ts: int | None) -> list[dict]:
    start_ts = _window_start_ms(trade_day, AUCTION_START)
    window_end_ts = _window_start_ms(trade_day, TRADE_END)
    end_ts = window_end_ts
    if as_of_ts is not None:
        end_ts = min(max(int(as_of_ts) + 1, start_ts), window_end_ts)
    return _read_quote_window_rows(data_dir, trade_day, start_ts=start_ts, end_ts=end_ts)


def _load_dynamic_asof_inputs(
    data_dir: Path,
    trade_day: date,
    *,
    as_of_ts: int,
) -> tuple[list[dict], dict, dict]:
    start_ts = _window_start_ms(trade_day, AUCTION_START)
    window_end_ts = _window_start_ms(trade_day, TRADE_END)
    end_ts = min(max(int(as_of_ts) + 1, start_ts), window_end_ts)
    entry = _quote_window_cache_entry(data_dir, trade_day)
    raw_rows = _filter_rows_by_time(entry.get("rows", []), start_ts=start_ts, end_ts=end_ts)
    cached_classified = entry.get("classified") or {}
    classified = {
        "auction_rows": _filter_rows_by_time(
            cached_classified.get("auction_rows", []),
            start_ts=start_ts,
            end_ts=end_ts,
        ),
        "trade_rows": _filter_rows_by_time(
            cached_classified.get("trade_rows", []),
            start_ts=start_ts,
            end_ts=end_ts,
        ),
        "invalid_trade_rows": _filter_rows_by_time(
            cached_classified.get("invalid_trade_rows", []),
            start_ts=start_ts,
            end_ts=end_ts,
        ),
    }
    hot_rows = [
        dict(row) for row in quote_tick_store._hot_rows(data_dir, target_date=trade_day)
        if start_ts <= int(row.get("event_ts") or 0) < end_ts
    ]
    if hot_rows:
        hot_classified = _classify_rows(hot_rows, trade_day)
        raw_rows = _dedupe_tick_rows(list(raw_rows) + hot_rows)
        raw_rows.sort(key=_row_rank)
        classified = {
            key: _merge_classified_rows(classified.get(key, []), hot_classified.get(key, []))
            for key in ("auction_rows", "trade_rows", "invalid_trade_rows")
        }
    quality = _quality(raw_rows, classified, set())
    return raw_rows, classified, quality


def _read_quote_window_rows(data_dir: Path, trade_day: date, *, start_ts: int, end_ts: int) -> list[dict]:
    window_start = _window_start_ms(trade_day, AUCTION_START)
    window_end = _window_start_ms(trade_day, TRADE_END)
    if start_ts >= window_start and end_ts <= window_end:
        parquet_rows = _quote_window_parquet_rows(data_dir, trade_day)
        rows = [
            row for row in parquet_rows
            if int(start_ts) <= int(row.get("event_ts") or 0) < int(end_ts)
        ]
    else:
        rows = _scan_quote_window_rows(data_dir, trade_day, start_ts=start_ts, end_ts=end_ts)
    hot_rows = [
        dict(row) for row in quote_tick_store._hot_rows(data_dir, target_date=trade_day)
        if int(start_ts) <= int(row.get("event_ts") or 0) < int(end_ts)
    ]
    rows = _dedupe_tick_rows(rows + hot_rows)
    rows.sort(key=_row_rank)
    return rows


def _filter_rows_by_time(rows: list[dict], *, start_ts: int, end_ts: int) -> list[dict]:
    return [
        row for row in rows
        if int(start_ts) <= int(row.get("event_ts") or 0) < int(end_ts)
    ]


def _merge_classified_rows(parquet_rows: list[dict], hot_rows: list[dict]) -> list[dict]:
    if not hot_rows:
        return list(parquet_rows)
    rows = _dedupe_tick_rows(list(parquet_rows) + hot_rows)
    rows.sort(key=_row_rank)
    return rows


def _freeze_jsonish(value):
    if isinstance(value, dict):
        return tuple(
            (str(key), _freeze_jsonish(val))
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_jsonish(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_jsonish(item) for item in value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _quote_window_parquet_rows(data_dir: Path, trade_day: date) -> list[dict]:
    entry = _quote_window_cache_entry(data_dir, trade_day)
    return list(entry.get("rows", []))


def _quote_window_cache_entry(data_dir: Path, trade_day: date) -> dict:
    base = data_dir / "quote_ticks" / f"date={trade_day.isoformat()}"
    cache_key = (str(Path(data_dir).resolve()), trade_day.isoformat())
    now = time.monotonic()
    with _quote_window_lock:
        cached = _quote_window_cache.get(cache_key)
        if (
            cached is not None
            and now - float(cached.get("checked_at") or 0.0) < QUOTE_WINDOW_FINGERPRINT_TTL
        ):
            return cached

    fingerprint = _quote_window_fingerprint(base)
    with _quote_window_lock:
        cached = _quote_window_cache.get(cache_key)
        if cached is not None and cached.get("fingerprint") == fingerprint:
            cached["checked_at"] = now
            return cached

    rows = _scan_quote_window_rows(
        data_dir,
        trade_day,
        start_ts=_window_start_ms(trade_day, AUCTION_START),
        end_ts=_window_start_ms(trade_day, TRADE_END),
        paths=[item[0] for item in fingerprint],
    )
    classified = _classify_rows(rows, trade_day)
    entry = {
        "fingerprint": fingerprint,
        "checked_at": now,
        "rows": rows,
        "classified": classified,
    }
    with _quote_window_lock:
        _quote_window_cache[cache_key] = entry
        if len(_quote_window_cache) > 4:
            oldest = next(iter(_quote_window_cache))
            _quote_window_cache.pop(oldest, None)
    return entry


def _quote_window_fingerprint(base: Path) -> tuple[tuple[str, int, int], ...]:
    try:
        paths = sorted(base.rglob("*.parquet")) if base.exists() else []
    except OSError:
        return ()
    items: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        items.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(items)


def _scan_quote_window_rows(
    data_dir: Path,
    trade_day: date,
    *,
    start_ts: int,
    end_ts: int,
    paths: list[str] | None = None,
) -> list[dict]:
    import polars as pl

    from app.parquet import scan_parquet_compat

    base = data_dir / "quote_ticks" / f"date={trade_day.isoformat()}"
    if paths is None:
        try:
            paths = [str(path) for path in sorted(base.rglob("*.parquet"))] if base.exists() else []
        except OSError:
            paths = []
    if not paths:
        return []
    try:
        frame = scan_parquet_compat(
            paths,
            schema=quote_tick_store.QUOTE_TICK_SCHEMA_OVERRIDES,
            hive_partitioning=False,
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        ).filter(
            (pl.col("event_ts") >= int(start_ts))
            & (pl.col("event_ts") < int(end_ts))
        )
        df = frame.collect(engine="streaming")
        return [dict(row) for row in df.iter_rows(named=True)]
    except Exception as exc:
        logger.warning("auction dynamic quote window read failed(%s): %s", base, exc)
        return []


def _dedupe_tick_rows(rows: list[dict]) -> list[dict]:
    deduped: dict[tuple[str, int, int, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("symbol") or "").upper(),
            int(row.get("event_ts") or 0),
            int(row.get("ingest_ts") or 0),
            str(row.get("source") or ""),
        )
        deduped[key] = row
    return list(deduped.values())


def _dynamic_timeline(classified: dict, trade_day: date) -> list[dict]:
    dense = _timeline(classified, trade_day)
    start_ts = _window_start_ms(trade_day, AUCTION_END)
    return [point for point in dense if int(point.get("ts") or 0) >= start_ts]


def _build_dynamic_frame(
    as_of_ts: int,
    repo,
    engine,
    history,
    *,
    signal_date: date,
    trade_day: date,
    strategy_ids: list[str],
    params_map: dict[str, dict],
    overrides_map: dict[str, dict],
    classified: dict,
    include_candidates: bool,
    asset_type: str,
    timeframe: str,
    point: dict | None = None,
) -> dict:
    import polars as pl

    auction_map = _latest_by_symbol(
        classified["auction_rows"],
        as_of_ts=min(as_of_ts, _window_start_ms(trade_day, AUCTION_END) - 1),
    )
    trade_map = _latest_by_symbol(classified["trade_rows"], as_of_ts=as_of_ts)
    snapshot_rows = _dynamic_snapshot_rows(
        history,
        auction_map,
        trade_map,
        signal_date=signal_date,
        trade_day=trade_day,
        as_of_ts=as_of_ts,
    )
    if not snapshot_rows:
        return _empty_dynamic_frame(
            as_of_ts,
            signal_date=signal_date,
            trade_day=trade_day,
            strategy_ids=strategy_ids,
            auction_map=auction_map,
            trade_map=trade_map,
            point=point,
            reason="empty_snapshot",
        )

    current = pl.DataFrame(snapshot_rows, infer_schema_length=None)
    if current.is_empty():
        return _empty_dynamic_frame(
            as_of_ts,
            signal_date=signal_date,
            trade_day=trade_day,
            strategy_ids=strategy_ids,
            auction_map=auction_map,
            trade_map=trade_map,
            point=point,
            reason="empty_snapshot",
        )

    history_for_matrix = history
    if "date" in history_for_matrix.columns:
        history_for_matrix = history_for_matrix.filter(pl.col("date") < trade_day)
    panel = pl.concat([history_for_matrix, current], how="diagonal_relaxed").sort(["symbol", "date"])
    from app.strategy.engine import StrategyDataContext

    context = StrategyDataContext(
        asset_type=asset_type,
        timeframe=timeframe,
        as_of=trade_day,
        current=current,
        history=history_for_matrix,
        cache_key=f"auction_dynamic:{asset_type}:{timeframe}:{trade_day.isoformat()}:{','.join(strategy_ids)}",
    )
    started = time.perf_counter()
    matrix = None
    try:
        matrix = engine.prepare_realtime_matrix(
            context,
            strategy_ids,
            params_map=params_map,
            overrides_map=overrides_map,
        )
    except Exception as exc:
        logger.warning("auction dynamic realtime matrix failed: %s", exc)

    run_context = StrategyDataContext(
        asset_type=asset_type,
        timeframe=timeframe,
        as_of=trade_day,
        current=current,
        history=panel,
        market=matrix,
        cache_key=context.cache_key,
    )
    results: dict[str, dict] = {}
    try:
        engine_results = engine.run_all(
            run_context,
            params_map=params_map,
            overrides_map=overrides_map,
            strategy_ids=strategy_ids,
        )
    except Exception as exc:
        logger.warning("auction dynamic strategy run failed: %s", exc)
        engine_results = {}

    matrix_stats = engine.realtime_matrix_stats(context.cache_key)
    for sid in strategy_ids:
        raw_result = engine_results.get(sid)
        if raw_result is None:
            results[sid] = _empty_dynamic_strategy_result(
                sid,
                signal_date=signal_date,
                trade_day=trade_day,
            )
            continue
        results[sid] = _dynamic_strategy_result(
            raw_result,
            auction_map,
            trade_map,
            as_of_ts=as_of_ts,
            signal_date=signal_date,
            trade_day=trade_day,
            include_candidates=include_candidates,
            matrix_stats=matrix_stats,
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "as_of_ts": as_of_ts,
        "as_of_time": _format_time(as_of_ts),
        "phase": _phase_at_ts(as_of_ts),
        "point": point,
        "auction_snapshot_ts": _max_event_ts(auction_map.values()),
        "auction_snapshot_time": _format_time(_max_event_ts(auction_map.values())),
        "auction_snapshot_stale_seconds": _stale_seconds(
            as_of_ts,
            _max_event_ts(auction_map.values()),
        ),
        "trade_snapshot_ts": _max_event_ts(trade_map.values()),
        "trade_snapshot_time": _format_time(_max_event_ts(trade_map.values())),
        "trade_snapshot_stale_seconds": _stale_seconds(
            as_of_ts,
            _max_event_ts(trade_map.values()),
        ),
        "auction_symbols": len(auction_map),
        "trade_symbols": len(trade_map),
        "snapshot_symbols": len(snapshot_rows),
        "elapsed_ms": elapsed_ms,
        "matrix_stats": matrix_stats,
        "results": results,
        "final_symbols": {
            sid: [
                str(row.get("symbol"))
                for row in result.get("rows", [])
                if row.get("symbol")
            ]
            for sid, result in results.items()
        },
    }


def _empty_dynamic_frame(
    as_of_ts: int,
    *,
    signal_date: date,
    trade_day: date,
    strategy_ids: list[str],
    auction_map: dict[str, dict],
    trade_map: dict[str, dict],
    point: dict | None,
    reason: str,
) -> dict:
    return {
        "as_of_ts": as_of_ts,
        "as_of_time": _format_time(as_of_ts),
        "phase": _phase_at_ts(as_of_ts),
        "point": point,
        "reason": reason,
        "auction_snapshot_ts": _max_event_ts(auction_map.values()),
        "auction_snapshot_time": _format_time(_max_event_ts(auction_map.values())),
        "trade_snapshot_ts": _max_event_ts(trade_map.values()),
        "trade_snapshot_time": _format_time(_max_event_ts(trade_map.values())),
        "auction_symbols": len(auction_map),
        "trade_symbols": len(trade_map),
        "snapshot_symbols": 0,
        "elapsed_ms": 0.0,
        "matrix_stats": {"generation": 0, "build_count": 0, "update_count": 0},
        "results": {
            sid: _empty_dynamic_strategy_result(
                sid,
                signal_date=signal_date,
                trade_day=trade_day,
            )
            for sid in strategy_ids
        },
        "final_symbols": {sid: [] for sid in strategy_ids},
    }


def _dynamic_snapshot_rows(
    history,
    auction_map: dict[str, dict],
    trade_map: dict[str, dict],
    *,
    signal_date: date,
    trade_day: date,
    as_of_ts: int,
) -> list[dict]:
    import polars as pl

    if history is None or history.is_empty() or "symbol" not in history.columns:
        return []
    hist = history.filter(history["date"] <= signal_date).sort(["symbol", "date"])
    if hist.is_empty():
        return []
    latest = hist.unique(subset=["symbol"], keep="last")
    prev5 = (
        hist.group_by("symbol", maintain_order=True)
        .tail(5)
        .group_by("symbol")
        .agg([
            pl.col("volume").mean().alias("_prev5_volume")
            if "volume" in hist.columns else pl.lit(None).alias("_prev5_volume"),
        ])
    )
    latest_rows = {
        str(row["symbol"]).upper(): row
        for row in latest.join(prev5, on="symbol", how="left").iter_rows(named=True)
    }
    projection_minutes = _auction_projection_minutes(trade_day, as_of_ts)
    projection_factor = 240.0 / projection_minutes
    rows: list[dict] = []
    symbols = sorted(set(auction_map) | set(trade_map))
    for symbol in symbols:
        base = latest_rows.get(symbol)
        if base is None:
            continue
        auction_row = auction_map.get(symbol)
        trade_row = trade_map.get(symbol)
        source = trade_row or auction_row
        if source is None:
            continue
        raw_close = _snapshot_price(source)
        if raw_close is None or raw_close <= 0:
            continue
        raw_open = _positive_float(source.get("open"))
        if raw_open is None and auction_row is not None:
            raw_open = _snapshot_price(auction_row)
        if raw_open is None:
            raw_open = raw_close
        raw_high = max(v for v in (
            _positive_float(source.get("high")),
            raw_open,
            raw_close,
        ) if v is not None)
        raw_low = min(v for v in (
            _positive_float(source.get("low")),
            raw_open,
            raw_close,
        ) if v is not None)
        actual_volume = _snapshot_volume(source)
        actual_amount = _snapshot_amount(source, raw_close, actual_volume)
        projected_volume = actual_volume * projection_factor
        projected_amount = actual_amount * projection_factor
        auction_result_price = _snapshot_price(auction_row) if auction_row is not None else None
        auction_result_volume = _snapshot_volume(auction_row) if auction_row is not None else None
        auction_result_amount = (
            _snapshot_amount(auction_row, auction_result_price, auction_result_volume)
            if auction_row is not None
            and auction_result_price is not None
            and auction_result_volume is not None
            else None
        )
        adj_factor = _adj_factor(base)
        close = raw_close * adj_factor
        prev_close = _prev_close(base, source, adj_factor)
        prev5_volume = _float_or_none(base.get("_prev5_volume"))
        vol_ratio = (
            projected_volume / prev5_volume
            if prev5_volume not in (None, 0) and projected_volume > 0
            else None
        )
        row = dict(base)
        row.update({
            "date": trade_day,
            "open": raw_open * adj_factor,
            "high": raw_high * adj_factor,
            "low": raw_low * adj_factor,
            "close": close,
            "volume": projected_volume,
            "amount": projected_amount,
            "raw_close": raw_close,
            "raw_high": raw_high,
            "raw_low": raw_low,
            "auction_result_price": (
                auction_result_price * adj_factor
                if auction_result_price is not None
                else None
            ),
            "auction_result_volume": auction_result_volume,
            "auction_result_amount": auction_result_amount,
            "prev_close": prev_close,
            "change_pct": (close / prev_close - 1.0) if prev_close not in (None, 0) else None,
            "change_amount": (close - prev_close) if prev_close is not None else None,
            "amplitude": ((raw_high - raw_low) / (prev_close / adj_factor))
            if prev_close not in (None, 0) and adj_factor not in (None, 0)
            else None,
            "vol_ratio_5d": vol_ratio,
            "snapshot_price": raw_close,
            "snapshot_open": raw_open,
            "snapshot_high": raw_high,
            "snapshot_low": raw_low,
            "snapshot_actual_volume": actual_volume,
            "snapshot_actual_amount": actual_amount,
            "snapshot_projected_volume": projected_volume,
            "snapshot_projected_amount": projected_amount,
            "snapshot_projection_minutes": projection_minutes,
            "snapshot_projection_factor": projection_factor,
            "snapshot_event_ts": int(source.get("event_ts") or 0) or None,
            "snapshot_event_time": _format_time(source.get("event_ts")),
            "snapshot_price_type": source.get("price_type"),
            "signal_limit_up": False,
            "signal_limit_down": False,
            "signal_broken_limit_up": False,
            "signal_limit_down_recovery": False,
        })
        rows.append(row)
    return rows


def _dynamic_strategy_result(
    raw_result,
    auction_map: dict[str, dict],
    trade_map: dict[str, dict],
    *,
    as_of_ts: int,
    signal_date: date,
    trade_day: date,
    include_candidates: bool,
    matrix_stats: dict,
) -> dict:
    result_dict = asdict(raw_result)
    raw_rows = [row for row in result_dict.get("rows") or [] if isinstance(row, dict)]
    dual_rows: list[dict] = []
    final_rows: list[dict] = []
    auction_total = 0
    trade_total = 0
    for row in raw_rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        auction_row = auction_map.get(symbol)
        trade_row = trade_map.get(symbol)
        if auction_row is not None:
            auction_total += 1
        if trade_row is not None:
            trade_total += 1
        enriched = _build_replay_row(row, auction_row, trade_row, as_of_ts=as_of_ts)
        dual_rows.append(enriched)
        if auction_row is not None and trade_row is not None:
            final_rows.append(enriched)
    dual_rows.sort(key=_candidate_sort_key, reverse=True)
    final_rows.sort(key=_result_sort_key, reverse=True)
    public = {
        "strategy": raw_result.strategy_id,
        "as_of": signal_date.isoformat(),
        "dynamic_as_of": trade_day.isoformat(),
        "trade_date": trade_day.isoformat(),
        "base_total": len(dual_rows),
        "candidate_total": len(dual_rows),
        "total": len(final_rows),
        "final_total": len(final_rows),
        "confirmed_total": len(final_rows),
        "auction_covered_total": auction_total,
        "trade_covered_total": trade_total,
        "pending_auction_total": max(len(dual_rows) - auction_total, 0),
        "pending_trade_total": max(len(dual_rows) - trade_total, 0),
        "rows": final_rows,
        "dual_rows": dual_rows,
        "elapsed_ms": raw_result.elapsed_ms,
        "entry_signal_hits": result_dict.get("entry_signal_hits") or [],
        "exit_signal_hits": result_dict.get("exit_signal_hits") or [],
        "run_meta": {
            "recomputed": True,
            "matrix_generation": matrix_stats.get("generation"),
            "matrix_build_count": matrix_stats.get("build_count"),
            "matrix_update_count": matrix_stats.get("update_count"),
        },
    }
    if include_candidates:
        public["candidates"] = dual_rows
    return public


def _empty_dynamic_strategy_result(sid: str, *, signal_date: date, trade_day: date) -> dict:
    return {
        "strategy": sid,
        "as_of": signal_date.isoformat(),
        "dynamic_as_of": trade_day.isoformat(),
        "trade_date": trade_day.isoformat(),
        "base_total": 0,
        "candidate_total": 0,
        "total": 0,
        "final_total": 0,
        "confirmed_total": 0,
        "auction_covered_total": 0,
        "trade_covered_total": 0,
        "pending_auction_total": 0,
        "pending_trade_total": 0,
        "rows": [],
        "dual_rows": [],
    }


def _dynamic_status(frame: dict, quality: dict) -> str:
    del quality
    if int(frame.get("snapshot_symbols") or 0) <= 0:
        return "missing_ticks"
    if int(frame.get("trade_symbols") or 0) <= 0:
        return "awaiting_trade"
    return "ready"


def _dynamic_quality(quality: dict, frame: dict) -> dict:
    out = dict(quality)
    out.update({
        "snapshot_symbols": frame.get("snapshot_symbols", 0),
        "auction_symbols_as_of": frame.get("auction_symbols", 0),
        "trade_symbols_as_of": frame.get("trade_symbols", 0),
        "matrix_stats": frame.get("matrix_stats"),
    })
    return out


def _auction_projection_minutes(trade_day: date, as_of_ts: int) -> float:
    start_ts = _window_start_ms(trade_day, AUCTION_END)
    end_ts = _window_start_ms(trade_day, TRADE_END)
    if as_of_ts <= start_ts:
        return 1.0
    if as_of_ts < end_ts:
        return max((as_of_ts - start_ts) / 60_000.0, 1.0)
    return 5.0


def _snapshot_price(row: dict) -> float | None:
    return (
        _positive_float(row.get("last_price"))
        or _positive_float(row.get("auction_price"))
        or _positive_float(row.get("close"))
    )


def _snapshot_volume(row: dict) -> float:
    return (
        _positive_float(row.get("volume"))
        or _positive_float(row.get("auction_matched_volume"))
        or 0.0
    )


def _snapshot_amount(row: dict, price: float, volume: float) -> float:
    amount = _positive_float(row.get("amount"))
    if amount is not None:
        return amount
    return price * volume * 100.0


def _adj_factor(base_row: dict) -> float:
    close = _positive_float(base_row.get("close"))
    raw_close = _positive_float(base_row.get("raw_close"))
    if close is None or raw_close is None:
        return 1.0
    return close / raw_close if raw_close > 0 else 1.0


def _prev_close(base_row: dict, source_row: dict, adj_factor: float) -> float | None:
    raw_prev = _positive_float(source_row.get("prev_close"))
    if raw_prev is not None:
        return raw_prev * adj_factor
    close = _positive_float(base_row.get("close"))
    return close


def _positive_float(value) -> float | None:
    out = _float_or_none(value)
    if out is None or out <= 0:
        return None
    return out


def _payload_base(status: str, signal_date: date, trade_day: date, now: datetime) -> dict:
    return {
        "status": status,
        "mode": "auction_replay",
        "as_of": signal_date.isoformat(),
        "trade_date": trade_day.isoformat(),
        "updated_at": _now_ms(now),
        "auction_window": {
            "start": AUCTION_START.strftime("%H:%M:%S"),
            "end": AUCTION_END.strftime("%H:%M:%S"),
        },
        "confirm_window": {
            "start": AUCTION_END.strftime("%H:%M:%S"),
            "end": TRADE_END.strftime("%H:%M:%S"),
        },
        "timeline_sparse": False,
        "missing_seconds_are_carried_forward": True,
    }


def _empty_payload(
    *,
    status: str,
    signal_date: date | None,
    trade_day: date,
    now: datetime,
) -> dict:
    base = {
        "status": status,
        "mode": "auction_replay",
        "as_of": signal_date.isoformat() if signal_date else None,
        "trade_date": trade_day.isoformat(),
        "updated_at": _now_ms(now),
        "timeline": [],
        "frames": [],
        "frame": None,
        "final_frame": None,
        "data_quality": {
            "requested_symbols": 0,
            "raw_rows": 0,
            "auction_rows": 0,
            "trade_rows": 0,
            "invalid_trade_rows": 0,
        },
        "timeline_sparse": False,
        "missing_seconds_are_carried_forward": True,
    }
    return base


def _stale_payload(
    *,
    signal_date: date,
    cached_as_of: date,
    trade_day: date,
    requested_ids: list[str],
    now: datetime,
) -> dict:
    results = {
        sid: _empty_strategy_result(sid, signal_date=signal_date, trade_day=trade_day)
        for sid in requested_ids
    }
    payload = _empty_payload(
        status="stale_as_of",
        signal_date=signal_date,
        trade_day=trade_day,
        now=now,
    )
    payload["cache_as_of"] = cached_as_of.isoformat()
    payload["final_frame"] = {
        "as_of_ts": None,
        "as_of_time": None,
        "phase": "stale_as_of",
        "results": results,
    }
    return payload


def _base_results(
    cache_results: dict,
    requested_ids: list[str],
    *,
    signal_date: date,
    trade_day: date,
) -> tuple[dict[str, dict], set[str]]:
    base_results: dict[str, dict] = {}
    all_symbols: set[str] = set()
    for sid in requested_ids:
        raw = cache_results.get(sid)
        raw_as_of = _parse_date(raw.get("as_of")) if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or raw_as_of != signal_date:
            base_results[sid] = _empty_strategy_result(
                sid,
                signal_date=signal_date,
                trade_day=trade_day,
            )
            continue
        rows = [row for row in (raw.get("rows") or []) if isinstance(row, dict)]
        base_results[sid] = {
            "strategy": sid,
            "as_of": signal_date.isoformat(),
            "trade_date": trade_day.isoformat(),
            "base_total": len(rows),
            "total": 0,
            "confirmed_total": 0,
            "auction_covered_total": 0,
            "trade_covered_total": 0,
            "pending_auction_total": len(rows),
            "pending_trade_total": len(rows),
            "rows": [],
            "_base_rows": rows,
        }
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if sym:
                all_symbols.add(sym)
    return base_results, all_symbols


def _empty_strategy_result(sid: str, *, signal_date: date, trade_day: date) -> dict:
    return {
        "strategy": sid,
        "as_of": signal_date.isoformat(),
        "trade_date": trade_day.isoformat(),
        "base_total": 0,
        "total": 0,
        "confirmed_total": 0,
        "auction_covered_total": 0,
        "trade_covered_total": 0,
        "pending_auction_total": 0,
        "pending_trade_total": 0,
        "rows": [],
    }


def _load_rows(data_dir: Path, trade_day: date, symbols: set[str]) -> list[dict]:
    if not symbols:
        return []
    try:
        return quote_tick_store.read_ticks(
            data_dir,
            target_date=trade_day,
            symbols=sorted(symbols),
        )
    except Exception as exc:
        logger.warning("auction replay quote_ticks read failed: %s", exc)
        return []


def _classify_rows(rows: list[dict], trade_day: date) -> dict:
    auction_start = _window_start_ms(trade_day, AUCTION_START)
    auction_end = _window_start_ms(trade_day, AUCTION_END)
    trade_end = _window_start_ms(trade_day, TRADE_END)
    auction_rows = []
    trade_rows = []
    invalid_trade_rows = []
    for row in rows:
        event_ts = int(row.get("event_ts") or 0)
        if auction_start <= event_ts < auction_end and _is_auction_row(row):
            auction_rows.append(row)
        elif auction_end <= event_ts < trade_end and _is_trade_row(row):
            if _float_or_none(row.get("last_price")) in (None, 0):
                invalid_trade_rows.append(row)
            else:
                trade_rows.append(row)
    auction_rows.sort(key=_row_rank)
    trade_rows.sort(key=_row_rank)
    return {
        "auction_rows": auction_rows,
        "trade_rows": trade_rows,
        "invalid_trade_rows": invalid_trade_rows,
    }


def _timeline(classified: dict, trade_day: date) -> list[dict]:
    by_second: dict[int, dict] = {}
    for kind in ("auction", "trade"):
        rows = classified[f"{kind}_rows"]
        for row in rows:
            event_ts = int(row.get("event_ts") or 0)
            second_ts = event_ts // 1000 * 1000
            point = by_second.setdefault(
                second_ts,
                {
                    "ts": second_ts,
                    "time": _format_time(second_ts),
                    "as_of_ts": event_ts,
                    "as_of_time": _format_time(event_ts),
                    "auction_event_count": 0,
                    "trade_event_count": 0,
                    "auction_symbol_count": 0,
                    "trade_symbol_count": 0,
                    "_auction_symbols": set(),
                    "_trade_symbols": set(),
                    "has_event": False,
                },
            )
            if event_ts > int(point["as_of_ts"]):
                point["as_of_ts"] = event_ts
                point["as_of_time"] = _format_time(event_ts)
            point[f"{kind}_event_count"] += 1
            point[f"_{kind}_symbols"].add(str(row.get("symbol") or "").upper())
            point["has_event"] = True

    dense: list[dict] = []
    start_ts = _window_start_ms(trade_day, AUCTION_START)
    auction_end_ts = _window_start_ms(trade_day, AUCTION_END)
    end_ts = _window_start_ms(trade_day, TRADE_END)
    current = start_ts
    while current < end_ts:
        second_end_ts = current + 999
        point = by_second.get(current)
        if point is None:
            point = {
                "ts": current,
                "time": _format_time(current),
                "as_of_ts": second_end_ts,
                "as_of_time": _format_time(current),
                "auction_event_count": 0,
                "trade_event_count": 0,
                "auction_symbol_count": 0,
                "trade_symbol_count": 0,
                "_auction_symbols": set(),
                "_trade_symbols": set(),
                "has_event": False,
            }
        else:
            point = dict(point)
            point["latest_event_ts"] = point["as_of_ts"]
            point["latest_event_time"] = point["as_of_time"]
            point["as_of_ts"] = second_end_ts
            point["as_of_time"] = _format_time(current)
        auction_symbols = point.pop("_auction_symbols")
        trade_symbols = point.pop("_trade_symbols")
        point["auction_symbol_count"] = len(auction_symbols)
        point["trade_symbol_count"] = len(trade_symbols)
        point["phase"] = "auction" if current < auction_end_ts else "open_confirm"
        dense.append(point)
        current += 1000
    return dense


def _build_frame(
    as_of_ts: int,
    base_results: dict[str, dict],
    classified: dict,
    *,
    include_candidates: bool,
    point: dict | None = None,
) -> dict:
    auction_map = _latest_by_symbol(classified["auction_rows"], as_of_ts=as_of_ts)
    trade_map = _latest_by_symbol(classified["trade_rows"], as_of_ts=as_of_ts)
    results = {}
    confirmed_symbols: dict[str, set[str]] = {}
    for sid, base in base_results.items():
        rows = [row for row in base.get("_base_rows") or [] if isinstance(row, dict)]
        confirmed_rows = []
        candidate_rows = []
        auction_total = 0
        trade_total = 0
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            auction_row = auction_map.get(symbol)
            trade_row = trade_map.get(symbol)
            if auction_row is not None:
                auction_total += 1
            if trade_row is not None:
                trade_total += 1
            enriched = _build_replay_row(row, auction_row, trade_row, as_of_ts=as_of_ts)
            if trade_row is not None and auction_row is not None:
                confirmed_rows.append(enriched)
            if include_candidates:
                candidate_rows.append(enriched)
        confirmed_rows.sort(key=_result_sort_key, reverse=True)
        candidate_rows.sort(key=_candidate_sort_key, reverse=True)
        public = {
            key: value
            for key, value in base.items()
            if key != "_base_rows"
        }
        public.update({
            "total": len(confirmed_rows),
            "confirmed_total": len(confirmed_rows),
            "auction_covered_total": auction_total,
            "trade_covered_total": trade_total,
            "pending_auction_total": max(len(rows) - auction_total, 0),
            "pending_trade_total": max(len(rows) - trade_total, 0),
            "rows": confirmed_rows,
        })
        if include_candidates:
            public["candidates"] = candidate_rows
        results[sid] = public
        confirmed_symbols[sid] = {
            str(row.get("symbol") or "").upper()
            for row in confirmed_rows
            if row.get("symbol")
        }
    return {
        "as_of_ts": as_of_ts,
        "as_of_time": _format_time(as_of_ts),
        "phase": _phase_at_ts(as_of_ts),
        "point": point,
        "auction_snapshot_ts": _max_event_ts(auction_map.values()),
        "auction_snapshot_time": _format_time(_max_event_ts(auction_map.values())),
        "auction_snapshot_stale_seconds": _stale_seconds(
            as_of_ts,
            _max_event_ts(auction_map.values()),
        ),
        "trade_snapshot_ts": _max_event_ts(trade_map.values()),
        "trade_snapshot_time": _format_time(_max_event_ts(trade_map.values())),
        "trade_snapshot_stale_seconds": _stale_seconds(
            as_of_ts,
            _max_event_ts(trade_map.values()),
        ),
        "auction_symbols": len(auction_map),
        "trade_symbols": len(trade_map),
        "results": results,
        "confirmed_symbols": {
            sid: sorted(symbols)
            for sid, symbols in confirmed_symbols.items()
        },
    }


def _build_replay_row(
    base_row: dict,
    auction_row: dict | None,
    trade_row: dict | None,
    *,
    as_of_ts: int,
) -> dict:
    row = dict(base_row)
    auction_price = (
        _float_or_none((auction_row or {}).get("auction_price"))
        or _float_or_none((auction_row or {}).get("last_price"))
    )
    trade_price = _float_or_none((trade_row or {}).get("last_price"))
    if auction_row is None:
        status = "pending_auction"
    elif trade_row is None:
        status = "pending_trade"
    else:
        status = "confirmed"
    row.update({
        "auction_replay_status": status,
        "auction_price": auction_price,
        "auction_change_pct": _float_or_none((auction_row or {}).get("auction_change_pct")),
        "auction_matched_volume": _float_or_none((auction_row or {}).get("auction_matched_volume")),
        "auction_unmatched_side": (auction_row or {}).get("auction_unmatched_side"),
        "auction_unmatched_volume": _float_or_none((auction_row or {}).get("auction_unmatched_volume")),
        "auction_pressure_score": _float_or_none((auction_row or {}).get("auction_pressure_score")),
        "auction_event_ts": int((auction_row or {}).get("event_ts") or 0) or None,
        "auction_event_time": _format_time((auction_row or {}).get("event_ts")),
        "auction_stale_seconds": _stale_seconds(
            as_of_ts,
            int((auction_row or {}).get("event_ts") or 0) or None,
        ),
        "open_confirm_price": trade_price,
        "open_confirm_change_pct": _float_or_none((trade_row or {}).get("change_pct")),
        "open_confirm_volume": _float_or_none((trade_row or {}).get("volume")),
        "open_confirm_amount": _float_or_none((trade_row or {}).get("amount")),
        "open_confirm_event_ts": int((trade_row or {}).get("event_ts") or 0) or None,
        "open_confirm_time": _format_time((trade_row or {}).get("event_ts")),
        "open_confirm_stale_seconds": _stale_seconds(
            as_of_ts,
            int((trade_row or {}).get("event_ts") or 0) or None,
        ),
        "open_confirm_vs_auction_pct": (
            trade_price / auction_price - 1
            if auction_price not in (None, 0) and trade_price is not None
            else None
        ),
    })
    return row


def _latest_by_symbol(rows: list[dict], *, as_of_ts: int) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        event_ts = int(row.get("event_ts") or 0)
        if event_ts > as_of_ts:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        prev = latest.get(symbol)
        if prev is None or _row_rank(row) >= _row_rank(prev):
            latest[symbol] = row
    return latest


def _quality(rows: list[dict], classified: dict, symbols: set[str]) -> dict:
    auction_rows = classified["auction_rows"]
    trade_rows = classified["trade_rows"]
    invalid_trade_rows = classified["invalid_trade_rows"]
    return {
        "requested_symbols": len(symbols),
        "raw_rows": len(rows),
        "auction_rows": len(auction_rows),
        "auction_symbols": _symbol_count(auction_rows),
        "trade_rows": len(trade_rows),
        "trade_symbols": _symbol_count(trade_rows),
        "invalid_trade_rows": len(invalid_trade_rows),
        "raw_time_range": _time_range(rows),
        "auction_time_range": _time_range(auction_rows),
        "trade_time_range": _time_range(trade_rows),
        "sources": sorted({
            str(row.get("source"))
            for row in rows
            if row.get("source")
        }),
    }


def _status_from_quality(quality: dict) -> str:
    auction_rows = int(quality.get("auction_rows") or 0)
    trade_rows = int(quality.get("trade_rows") or 0)
    if auction_rows <= 0 and trade_rows <= 0:
        return "missing_ticks"
    if auction_rows <= 0 or trade_rows <= 0:
        return "partial_ticks"
    return "ready"


def _symbol_count(rows: list[dict]) -> int:
    return len({
        str(row.get("symbol") or "").strip().upper()
        for row in rows
        if row.get("symbol")
    })


def _time_range(rows: list[dict]) -> dict | None:
    values = [int(row.get("event_ts") or 0) for row in rows]
    values = [value for value in values if value > 0]
    if not values:
        return None
    return {
        "start_ts": min(values),
        "start": _format_time(min(values)),
        "end_ts": max(values),
        "end": _format_time(max(values)),
    }


def _result_sort_key(row: dict) -> tuple[float, float, float, float, str]:
    return (
        _sort_value(row.get("open_confirm_change_pct")),
        _sort_value(row.get("auction_change_pct")),
        _sort_value(row.get("auction_pressure_score")),
        _sort_value(row.get("score")),
        str(row.get("symbol") or ""),
    )


def _candidate_sort_key(row: dict) -> tuple[int, float, float, float, str]:
    status_rank = {
        "confirmed": 2,
        "pending_trade": 1,
        "pending_auction": 0,
    }.get(str(row.get("auction_replay_status")), -1)
    return (
        status_rank,
        _sort_value(row.get("open_confirm_change_pct")),
        _sort_value(row.get("auction_change_pct")),
        _sort_value(row.get("score")),
        str(row.get("symbol") or ""),
    )


def _sort_value(value) -> float:
    if value is None:
        return float("-inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def _row_rank(row: dict) -> tuple[int, int]:
    return (
        int(row.get("event_ts") or 0),
        int(row.get("ingest_ts") or 0),
    )


def _max_event_ts(rows) -> int | None:
    values = [int(row.get("event_ts") or 0) for row in rows if row]
    values = [value for value in values if value > 0]
    return max(values) if values else None


def _is_auction_row(row: dict) -> bool:
    return row.get("price_type") == "auction_reference" or row.get("market_phase") == "preopen_auction"


def _is_trade_row(row: dict) -> bool:
    return row.get("price_type") != "auction_reference"


def _normalize_strategy_ids(strategy_ids: list[str] | None) -> list[str] | None:
    if strategy_ids is None:
        return None
    out = []
    for sid in strategy_ids:
        text = str(sid or "").strip()
        if text:
            out.append(text)
    return out


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _window_start_ms(trade_day: date, tm: dt_time) -> int:
    return int(datetime.combine(trade_day, tm, tzinfo=CN_TZ).timestamp() * 1000)


def _phase_at_ts(event_ts: int) -> str:
    text = _format_time(event_ts)
    if text is None:
        return "unknown"
    return "auction" if text < AUCTION_END.strftime("%H:%M:%S") else "open_confirm"


def _stale_seconds(as_of_ts: int, source_ts: int | None) -> int | None:
    if source_ts is None:
        return None
    return max(0, int(as_of_ts - source_ts) // 1000)


def _format_time(event_ts) -> str | None:
    if event_ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(event_ts) / 1000, tz=CN_TZ)
    except (TypeError, ValueError, OSError):
        return None
    return dt.strftime("%H:%M:%S")


def _now_ms(now: datetime) -> int:
    return int(now.timestamp() * 1000)


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
