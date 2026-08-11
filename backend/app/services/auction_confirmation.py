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
            "auction_covered_total": auction_total,
            "trade_covered_total": trade_total,
            "pending_auction_total": max(len(rows) - auction_total, 0),
            "pending_trade_total": max(len(rows) - trade_total, 0),
            "rows": confirmed_rows,
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
