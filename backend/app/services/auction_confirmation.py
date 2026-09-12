"""盘后策略的竞价 / 开盘确认服务。"""
from __future__ import annotations

import logging
from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from app.market_time import cn_today
from app.services import quote_tick_store

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
AUCTION_START = dt_time(9, 23)
AUCTION_END = dt_time(9, 25)
TRADE_END = dt_time(9, 30)


def confirm_cached_strategy_results(
    data_dir,
    cached: dict,
    *,
    as_of: date | None = None,
    trade_date: date | None = None,
    strategy_ids: list[str] | None = None,
    params_map: dict[str, dict] | None = None,
) -> dict:
    """把盘后策略缓存与 09:23-09:25 / 09:25-09:30 快照拼成确认结果。"""
    now = datetime.now(tz=CN_TZ)
    cached_as_of = _parse_date(cached.get("as_of")) if cached.get("as_of") else None
    signal_date = as_of or cached_as_of
    trade_day = trade_date or cn_today()
    cache_results = cached.get("results") or {}

    if signal_date is None:
        return {
            "as_of": None,
            "trade_date": trade_day.isoformat(),
            "gate_status": "no_cache",
            "updated_at": _now_ms(now),
            "results": {},
        }

    requested_ids = _normalize_strategy_ids(strategy_ids)
    if requested_ids is None:
        requested_ids = [
            sid for sid, result in cache_results.items()
            if isinstance(result, dict)
        ]

    if params_map is None:
        try:
            from app.strategy import config as strategy_config
            all_overrides = strategy_config.list_overrides(data_dir)
            params_map = {
                sid: dict((all_overrides.get(sid) or {}).get("params") or {})
                for sid in requested_ids
            }
        except Exception:
            params_map = {}

    if cached_as_of is not None and cached_as_of != signal_date:
        return {
            "as_of": signal_date.isoformat(),
            "cache_as_of": cached_as_of.isoformat(),
            "trade_date": trade_day.isoformat(),
            "gate_status": "stale_as_of",
            "updated_at": _now_ms(now),
            "results": {
                sid: {
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
                for sid in requested_ids
            },
        }

    if not requested_ids:
        return {
            "as_of": signal_date.isoformat(),
            "trade_date": trade_day.isoformat(),
            "gate_status": "empty_candidates",
            "updated_at": _now_ms(now),
            "results": {},
        }

    base_results: dict[str, dict] = {}
    all_symbols: set[str] = set()
    for sid in requested_ids:
        raw = cache_results.get(sid)
        raw_as_of = _parse_date(raw.get("as_of")) if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or raw_as_of != signal_date:
            base_results[sid] = {
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
        }
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if sym:
                all_symbols.add(sym)

    gate_status = _gate_status(now, trade_day)
    if gate_status == "pending_gate":
        return {
            "as_of": signal_date.isoformat(),
            "trade_date": trade_day.isoformat(),
            "gate_status": gate_status,
            "updated_at": _now_ms(now),
            "auction_window": {
                "start": AUCTION_START.strftime("%H:%M:%S"),
                "end": AUCTION_END.strftime("%H:%M:%S"),
            },
            "confirm_window": {
                "start": AUCTION_END.strftime("%H:%M:%S"),
                "end": TRADE_END.strftime("%H:%M:%S"),
            },
            "results": base_results,
        }

    auction_map: dict[str, dict] = {}
    trade_map: dict[str, dict] = {}
    if all_symbols:
        rows = quote_tick_store.read_ticks(
            data_dir,
            target_date=trade_day,
            symbols=sorted(all_symbols),
        )
        auction_map = _latest_rows(
            rows,
            start_ms=_window_start_ms(trade_day, AUCTION_START),
            end_ms=_window_start_ms(trade_day, AUCTION_END),
            predicate=lambda row: _is_auction_row(row),
        )
        trade_map = _latest_rows(
            rows,
            start_ms=_window_start_ms(trade_day, AUCTION_END),
            end_ms=_window_start_ms(trade_day, TRADE_END),
            predicate=lambda row: _is_trade_row(row),
        )

    confirmed_any = False
    trade_rows_total = 0
    auction_rows_total = 0
    for sid in requested_ids:
        raw = cache_results.get(sid)
        rows = [row for row in (raw.get("rows") or []) if isinstance(row, dict)] if isinstance(raw, dict) else []
        confirmed_rows: list[dict] = []
        rejected_rows: list[dict] = []
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
            if auction_row is None or trade_row is None:
                continue

            # 校验策略确认条件，不满足者坚决淘汰，严禁误报确认买入
            is_ok, reason = _evaluate_candidate_confirmation(
                sid, row, auction_row, trade_row, params_map.get(sid) if params_map else None,
            )
            if not is_ok:
                logger.info("竞价确认淘汰 [%s] %s: %s", sid, symbol, reason)
                rejected_rows.append(_build_rejected_row(row, auction_row, trade_row, reason))
                continue

            confirmed_rows.append(_build_confirmed_row(row, auction_row, trade_row))

        confirmed_rows.sort(key=_result_sort_key, reverse=True)
        confirmed_any = confirmed_any or bool(confirmed_rows)
        trade_rows_total += trade_total
        auction_rows_total += auction_total
        base = base_results[sid]
        base.update({
            "base_total": len(rows),
            "total": len(confirmed_rows),
            "confirmed_total": len(confirmed_rows),
            "rejected_total": len(rejected_rows),
            "auction_covered_total": auction_total,
            "trade_covered_total": trade_total,
            "pending_auction_total": max(len(rows) - auction_total, 0),
            "pending_trade_total": max(len(rows) - trade_total, 0),
            "rows": confirmed_rows,
            "rejected_rows": rejected_rows,
        })

    if trade_rows_total <= 0:
        gate_status = "awaiting_trade"
    elif confirmed_any:
        gate_status = "confirmed"
    else:
        gate_status = "confirmed"

    return {
        "as_of": signal_date.isoformat(),
        "trade_date": trade_day.isoformat(),
        "gate_status": gate_status,
        "updated_at": _now_ms(now),
        "auction_window": {
            "start": AUCTION_START.strftime("%H:%M:%S"),
            "end": AUCTION_END.strftime("%H:%M:%S"),
        },
        "confirm_window": {
            "start": AUCTION_END.strftime("%H:%M:%S"),
            "end": TRADE_END.strftime("%H:%M:%S"),
        },
        "auction_rows_total": auction_rows_total,
        "trade_rows_total": trade_rows_total,
        "results": base_results,
    }


def _gate_status(now: datetime, trade_day: date) -> str:
    if trade_day == cn_today() and now.time() < AUCTION_END:
        return "pending_gate"
    return "open"


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


def _now_ms(now: datetime) -> int:
    return int(now.timestamp() * 1000)


def _latest_rows(
    rows: list[dict],
    *,
    start_ms: int,
    end_ms: int,
    predicate,
) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        event_ts = int(row.get("event_ts") or 0)
        if event_ts < start_ms or event_ts >= end_ms:
            continue
        if not predicate(row):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        prev = latest.get(symbol)
        if prev is None or _row_rank(row) >= _row_rank(prev):
            latest[symbol] = row
    return latest


def _row_rank(row: dict) -> tuple[int, int]:
    return (
        int(row.get("event_ts") or 0),
        int(row.get("ingest_ts") or 0),
    )


def _is_auction_row(row: dict) -> bool:
    return row.get("price_type") == "auction_reference" or row.get("market_phase") == "preopen_auction"


def _is_trade_row(row: dict) -> bool:
    return row.get("price_type") != "auction_reference"


def _build_confirmed_row(base_row: dict, auction_row: dict, trade_row: dict) -> dict:
    row = dict(base_row)
    auction_price = _float_or_none(auction_row.get("auction_price")) or _float_or_none(auction_row.get("last_price"))
    trade_price = _float_or_none(trade_row.get("last_price"))
    row.update({
        "auction_price": auction_price,
        "auction_change_pct": _float_or_none(auction_row.get("auction_change_pct")),
        "auction_matched_volume": _float_or_none(auction_row.get("auction_matched_volume")),
        "auction_unmatched_side": auction_row.get("auction_unmatched_side"),
        "auction_unmatched_volume": _float_or_none(auction_row.get("auction_unmatched_volume")),
        "auction_pressure_score": _float_or_none(auction_row.get("auction_pressure_score")),
        "auction_event_ts": int(auction_row.get("event_ts") or 0),
        "auction_event_time": _format_time(auction_row.get("event_ts")),
        "open_confirm_price": trade_price,
        "open_confirm_change_pct": _float_or_none(trade_row.get("change_pct")),
        "open_confirm_volume": _float_or_none(trade_row.get("volume")),
        "open_confirm_amount": _float_or_none(trade_row.get("amount")),
        "open_confirm_event_ts": int(trade_row.get("event_ts") or 0),
        "open_confirm_time": _format_time(trade_row.get("event_ts")),
        "open_confirm_vs_auction_pct": (
            trade_price / auction_price - 1
            if auction_price not in (None, 0) and trade_price is not None
            else None
        ),
        "auction_confirmation_status": "confirmed",
    })
    return row


def _build_rejected_row(
    base_row: dict,
    auction_row: dict | None,
    trade_row: dict | None,
    reason: str,
) -> dict:
    row = dict(base_row)
    if auction_row and trade_row:
        row = _build_confirmed_row(base_row, auction_row, trade_row)
    row.update({
        "auction_confirmation_status": "rejected",
        "auction_rejection_reason": reason,
    })
    return row


def _evaluate_candidate_confirmation(
    strategy_id: str,
    base_row: dict,
    auction_row: dict | None,
    trade_row: dict | None,
    params: dict | None = None,
) -> tuple[bool, str]:
    """校验候选标的是否真正满足该策略的竞价/开盘确认条件。

    返回 (is_confirmed, reason)。
    """
    if auction_row is None:
        return False, "未取得 09:25 前竞价快照，无法确认"
    if trade_row is None:
        return False, "未取得 09:25~09:30 开盘快照，无法确认"

    base_price = (
        _float_or_none(base_row.get("close"))
        or _float_or_none(base_row.get("price"))
        or _float_or_none(base_row.get("prev_close"))
    )
    auction_price = _float_or_none(auction_row.get("auction_price")) or _float_or_none(auction_row.get("last_price"))
    trade_price = _float_or_none(trade_row.get("last_price"))
    params = params or {}

    # 双刃合家族策略: 严格校验高开幅度
    if strategy_id in {
        "custom_dual_edge",
        "custom_dual_edge_focus",
        "custom_dual_edge_prime",
        "custom_dual_edge_v2",
        "custom_dual_edge_v3",
    }:
        if base_price in (None, 0) or auction_price is None or trade_price is None:
            return False, "竞价/开盘价格数据不完整，无法确认"
        open_value = _float_or_none(trade_row.get("open")) or auction_price or trade_price
        if open_value is None or open_value <= 0:
            return False, "开盘价格数据无效"

        open_gap = (open_value / base_price) - 1.0
        default_gap_max = 4.0 if strategy_id == "custom_dual_edge_prime" else 3.5
        gap_min = float(params.get("gap_min", 2.0)) / 100.0
        gap_max = float(params.get("gap_max", default_gap_max)) / 100.0
        if open_gap < gap_min:
            return False, f"竞价开盘 {open_gap * 100:+.2f}%，低于最低高开 {gap_min * 100:.1f}%"
        if open_gap > gap_max:
            return False, f"竞价开盘 {open_gap * 100:+.2f}%，超过最高高开 {gap_max * 100:.1f}%"
        return True, "竞价高开达标"

    # 断板弱开反包: 要求弱开/平开 (高开不超过 2%)
    if strategy_id == "custom_broken_board_weak_open":
        if base_price in (None, 0) or auction_price is None or trade_price is None:
            return False, "竞价/开盘价格数据不完整，无法确认"
        open_value = _float_or_none(trade_row.get("open")) or auction_price or trade_price
        if open_value is None or open_value <= 0:
            return False, "开盘价格数据无效"

        open_gap = (open_value / base_price) - 1.0
        gap_max = float(params.get("gap_max", 2.0)) / 100.0
        if open_gap > gap_max:
            return False, f"开盘高开 {open_gap * 100:+.2f}%，超过弱开上限 {gap_max * 100:.1f}%"
        if open_gap < -0.05:
            return False, f"开盘低开 {open_gap * 100:+.2f}%，跌幅过大超过 -5.0%"
        return True, "弱开达标"

    # 默认/其他普通策略: 开盘数据齐全即通过
    return True, "开盘数据就绪"


def _result_sort_key(row: dict) -> tuple[float, float, float, float, str]:
    return (
        _sort_value(row.get("open_confirm_change_pct")),
        _sort_value(row.get("auction_change_pct")),
        _sort_value(row.get("auction_pressure_score")),
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


def _format_time(event_ts) -> str | None:
    if event_ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(event_ts) / 1000, tz=CN_TZ)
    except (TypeError, ValueError, OSError):
        return None
    return dt.strftime("%H:%M:%S")


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
