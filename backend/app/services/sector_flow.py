"""板块强度与资金流时间序列聚合。"""
from __future__ import annotations

import math
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl

from app.market_time import cn_today
from app.services import quote_tick_store
from app.services.sector_monitor import SectorMonitorService

SectorKind = Literal["concept", "industry"]
Metric = Literal["strength", "main_flow"]

CORE_INDEX = {"symbol": "000001.SH", "name": "上证指数"}
FLOW_SOURCE_TRADE_TICKS = "trade_ticks"
FLOW_SOURCE_ACTIVE_VOLUME = "active_volume_estimate"
FLOW_SOURCE_AMOUNT_MOMENTUM = "amount_momentum_estimate"
_SERIES_CACHE_TTL_SECONDS = 15.0
_RECENT_SERIES_MAX_FILES = 96
_series_cache_lock = threading.Lock()
_series_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


def build_sector_flow_series(
    *,
    repo,
    sector_service: SectorMonitorService,
    kind: SectorKind,
    metric: Metric,
    trade_date: date,
    step_seconds: int = 60,
    limit: int = 24,
    level: int | None = None,
) -> dict[str, Any]:
    """返回板块列表、分时曲线和上证指数参考线。"""
    data_dir = Path(repo.store.data_dir)
    cache_key = (
        str(data_dir),
        kind,
        metric,
        trade_date.isoformat(),
        int(step_seconds),
        int(limit),
        level,
    )
    now = time.monotonic()
    with _series_cache_lock:
        cached = _series_cache.get(cache_key)
        if cached is not None and now - cached[0] <= _SERIES_CACHE_TTL_SECONDS:
            return cached[1]

    targets = _targets(sector_service, kind, level=level)
    members_by_key = sector_service.members_for_targets([target["key"] for target in targets])
    if not targets:
        payload = {
            "as_of": trade_date.isoformat(),
            "kind": kind,
            "metric": metric,
            "points": [],
            "sectors": [],
            "index": _empty_index(),
            "data_quality": {
                "status": "empty_targets",
                "has_ticks": False,
                "symbol_count": 0,
                "sources": [],
            },
        }
        _cache_series(cache_key, payload)
        return payload

    all_symbols = sorted({
        symbol
        for target in targets
        for symbol in members_by_key.get(target["key"], set())
    } | {CORE_INDEX["symbol"]})
    ticks = quote_tick_store.read_sampled_ticks(
        data_dir,
        target_date=trade_date,
        symbols=all_symbols,
        step_seconds=step_seconds,
        max_files=_RECENT_SERIES_MAX_FILES if trade_date == cn_today() else None,
    )
    points = _points_from_ticks(ticks, step_seconds=step_seconds)
    if not ticks:
        payload = {
            "as_of": trade_date.isoformat(),
            "kind": kind,
            "metric": metric,
            "points": [],
            "sectors": [],
            "index": _empty_index(),
            "data_quality": {
                "status": "empty",
                "has_ticks": False,
                "symbol_count": 0,
                "sources": [],
            },
        }
        _cache_series(cache_key, payload)
        return payload

    frame = _prepare_frame(ticks, points)
    if frame.is_empty():
        payload = {
            "as_of": trade_date.isoformat(),
            "kind": kind,
            "metric": metric,
            "points": points,
            "sectors": [],
            "index": _empty_index(),
            "data_quality": {
                "status": "empty_frame",
                "has_ticks": bool(ticks),
                "symbol_count": _symbol_count(ticks),
                "sources": _sources(ticks),
            },
        }
        _cache_series(cache_key, payload)
        return payload

    by_symbol = _rows_by_symbol(frame)
    sectors = _build_sector_items(targets, members_by_key, by_symbol, points)
    sectors = [
        sector for sector in sectors
        if (sector["latest_strength"] is not None or sector["latest_flow"] is not None)
    ]
    sort_key = "latest_strength" if metric == "strength" else "latest_flow"
    sectors.sort(key=lambda item: _sort_value(item.get(sort_key)), reverse=True)
    sectors = sectors[: max(1, min(int(limit or 24), 120))]
    for idx, item in enumerate(sectors, start=1):
        item["rank"] = idx

    payload = {
        "as_of": trade_date.isoformat(),
        "kind": kind,
        "metric": metric,
        "points": points,
        "sectors": sectors,
        "index": _index_series(by_symbol.get(CORE_INDEX["symbol"], []), points),
        "data_quality": {
            "status": "ready" if sectors else "empty_sectors",
            "has_ticks": bool(ticks),
            "symbol_count": _symbol_count(ticks),
            "sources": _sources(ticks),
            "flow_source": _overall_flow_source(sectors),
            "is_proxy": any(sector.get("is_proxy") for sector in sectors),
        },
    }
    _cache_series(cache_key, payload)
    return payload


def _cache_series(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    now = time.monotonic()
    with _series_cache_lock:
        _series_cache[key] = (now, payload)
        if len(_series_cache) > 64:
            oldest = sorted(_series_cache.items(), key=lambda item: item[1][0])[:16]
            for old_key, _ in oldest:
                _series_cache.pop(old_key, None)


def _targets(sector_service: SectorMonitorService, kind: SectorKind, *, level: int | None) -> list[dict]:
    items = [dict(target) for target in sector_service.list_targets().get(kind, [])]
    if kind == "industry" and level is not None:
        items = [item for item in items if item.get("level") == level]
    return [item for item in items if item.get("available", True)]


def _prepare_frame(rows: list[dict], points: list[int]) -> pl.DataFrame:
    keep = [
        "symbol", "event_ts", "last_price", "prev_close", "change_pct", "amount",
        "outside_volume", "inside_volume", "active_net_volume", "source",
    ]
    df = pl.DataFrame(rows, infer_schema_length=None)
    if df.is_empty() or "symbol" not in df.columns or "event_ts" not in df.columns:
        return pl.DataFrame()
    for column in keep:
        if column not in df.columns:
            dtype = pl.Utf8 if column in {"symbol", "source"} else pl.Float64
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(column))
    frame = (
        df.select(keep)
        .filter(pl.col("symbol").is_not_null() & pl.col("event_ts").is_not_null())
        .filter(pl.col("event_ts") <= max(points))
        .with_columns([
            pl.col("symbol").cast(pl.Utf8).str.strip_chars().str.to_uppercase(),
            pl.col("event_ts").cast(pl.Int64, strict=False),
            pl.col("last_price").cast(pl.Float64, strict=False),
            pl.col("prev_close").cast(pl.Float64, strict=False),
            pl.col("change_pct").cast(pl.Float64, strict=False),
            pl.col("amount").cast(pl.Float64, strict=False),
            pl.col("outside_volume").cast(pl.Float64, strict=False),
            pl.col("inside_volume").cast(pl.Float64, strict=False),
            pl.col("active_net_volume").cast(pl.Float64, strict=False),
        ])
        .sort(["symbol", "event_ts"])
    )
    return frame.with_columns(
        pl.when(pl.col("change_pct").is_null() & (pl.col("prev_close") > 0) & pl.col("last_price").is_not_null())
        .then(pl.col("last_price") / pl.col("prev_close") - 1)
        .otherwise(pl.col("change_pct"))
        .alias("change_pct")
    )


def _rows_by_symbol(frame: pl.DataFrame) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for row in frame.iter_rows(named=True):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        result.setdefault(symbol, []).append(row)
    return result


def _build_sector_items(
    targets: list[dict],
    members_by_key: dict[str, set[str]],
    by_symbol: dict[str, list[dict]],
    points: list[int],
) -> list[dict]:
    out = []
    for target in targets:
        members = sorted(members_by_key.get(target["key"], set()))
        if not members:
            continue
        member_series = [by_symbol[symbol] for symbol in members if symbol in by_symbol]
        valid_count = len(member_series)
        total_count = len(members)
        if valid_count == 0:
            continue
        flow_values: list[float | None] = []
        strength_values: list[float | None] = []
        flow_sources: list[str] = []
        proxy_flags: list[bool] = []
        cumulative_flow = 0.0
        previous_by_symbol: dict[str, tuple[float, str]] = {}
        rows_by_symbol = {
            str(rows[0].get("symbol") or ""): _align_rows(rows, points)
            for rows in member_series
            if rows
        }
        for point_index, point in enumerate(points):
            latest_rows = [
                rows[point_index]
                for rows in rows_by_symbol.values()
                if rows[point_index] is not None
            ]
            flow = 0.0
            point_sources: list[str] = []
            point_is_proxy = False
            has_flow = False
            for row in latest_rows:
                value, source, is_proxy = _row_flow_value(row)
                if value is None or source is None:
                    continue
                previous = previous_by_symbol.get(str(row.get("symbol") or ""))
                delta = value if previous is None or previous[1] != source else value - previous[0]
                previous_by_symbol[str(row.get("symbol") or "")] = (value, source)
                flow += delta
                point_sources.append(source)
                point_is_proxy = point_is_proxy or is_proxy
                has_flow = True
            strength = _strength_value(latest_rows)
            if has_flow:
                cumulative_flow += flow
            flow_values.append(cumulative_flow if has_flow else None)
            strength_values.append(strength)
            flow_sources.extend(point_sources)
            proxy_flags.append(point_is_proxy)
        latest_flow = _last_number(flow_values)
        latest_strength = _last_number(strength_values)
        out.append({
            "key": target["key"],
            "name": target.get("name") or target.get("value") or target["key"],
            "source_field": target.get("source_field"),
            "value": target.get("value"),
            "level": target.get("level"),
            "member_count": total_count,
            "valid_count": valid_count,
            "coverage_ratio": valid_count / total_count if total_count else 0,
            "flow_values": flow_values,
            "strength_values": strength_values,
            "latest_flow": latest_flow,
            "latest_strength": latest_strength,
            "flow_source": _dominant(flow_sources) or FLOW_SOURCE_AMOUNT_MOMENTUM,
            "classification_method": "trade_side_or_proxy",
            "is_proxy": any(proxy_flags),
        })
    return out


def _align_rows(rows: list[dict], points: list[int]) -> list[dict | None]:
    aligned: list[dict | None] = []
    row_index = 0
    current: dict | None = None
    for point in points:
        while row_index < len(rows) and int(rows[row_index].get("event_ts") or 0) <= point:
            current = rows[row_index]
            row_index += 1
        aligned.append(current)
    return aligned


def _latest_before(rows: list[dict], point: int) -> dict | None:
    candidate = None
    for row in rows:
        event_ts = int(row.get("event_ts") or 0)
        if event_ts > point:
            break
        candidate = row
    return candidate


def _row_flow_value(row: dict) -> tuple[float | None, str | None, bool]:
    price = _finite(row.get("last_price"))
    active_net_volume = _finite(row.get("active_net_volume"))
    outside_volume = _finite(row.get("outside_volume"))
    inside_volume = _finite(row.get("inside_volume"))
    if active_net_volume is None and outside_volume is not None and inside_volume is not None:
        active_net_volume = outside_volume - inside_volume
    if price is not None and active_net_volume is not None:
        return active_net_volume * price * 100, FLOW_SOURCE_ACTIVE_VOLUME, True
    amount = _finite(row.get("amount"))
    change_pct = _finite(row.get("change_pct"))
    if amount is not None and change_pct is not None:
        return amount * max(-1.0, min(1.0, change_pct * 20)), FLOW_SOURCE_AMOUNT_MOMENTUM, True
    return None, None, True


def _strength_value(rows: list[dict]) -> float | None:
    changes = [_finite(row.get("change_pct")) for row in rows]
    changes = [value for value in changes if value is not None]
    if not changes:
        return None
    amounts = [_finite(row.get("amount")) or 0 for row in rows]
    total_amount = sum(amounts)
    if total_amount > 0 and len(amounts) == len(rows):
        weighted = 0.0
        used_weight = 0.0
        for row, amount in zip(rows, amounts, strict=False):
            change = _finite(row.get("change_pct"))
            if change is None:
                continue
            weighted += change * amount
            used_weight += amount
        avg_change = weighted / used_weight if used_weight > 0 else sum(changes) / len(changes)
    else:
        avg_change = sum(changes) / len(changes)
    up_rate = sum(1 for value in changes if value > 0) / len(changes)
    return (avg_change * 100) * 0.72 + (up_rate - 0.5) * 8


def _index_series(rows: list[dict], points: list[int]) -> dict:
    values: list[float | None] = []
    for point in points:
        row = _latest_before(rows, point)
        values.append(_finite(row.get("change_pct")) if row else None)
    return {
        "symbol": CORE_INDEX["symbol"],
        "name": CORE_INDEX["name"],
        "values": values,
    }


def _empty_index() -> dict:
    return {"symbol": CORE_INDEX["symbol"], "name": CORE_INDEX["name"], "values": []}


def _points_from_ticks(rows: list[dict], *, step_seconds: int) -> list[int]:
    if not rows:
        return []
    step_ms = max(30, int(step_seconds or 60)) * 1000
    buckets: set[int] = set()
    for row in rows:
        event_ts = row.get("event_ts")
        if event_ts is None:
            continue
        try:
            ts = int(event_ts)
        except (TypeError, ValueError):
            continue
        buckets.add(ts // step_ms * step_ms)
    return sorted(buckets)


def _symbol_count(rows: list[dict]) -> int:
    return len({
        str(row.get("symbol") or "").strip().upper()
        for row in rows
        if str(row.get("symbol") or "").strip()
    })


def _sources(rows: list[dict]) -> list[str]:
    return sorted({
        str(row.get("source") or "").strip()
        for row in rows
        if str(row.get("source") or "").strip()
    })


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _last_number(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None and math.isfinite(value):
            return value
    return None


def _sort_value(value: Any) -> float:
    number = _finite(value)
    return number if number is not None else float("-inf")


def _dominant(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def _overall_flow_source(sectors: list[dict]) -> str | None:
    return _dominant([str(sector.get("flow_source")) for sector in sectors if sector.get("flow_source")])
