"""秒级行情事实层。

只记录系统从实时源看到的价格事实, 不表达买卖建议。当前决策台要求依赖
tdxapi, 因此 QuoteService 只会把 tdxapi 实时记录写入这里。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
FLUSH_INTERVAL_S = 3.0
FLUSH_BATCH_SIZE = 5000
RING_MAX_ROWS = 20000
STALE_MS = 15_000
DEPTH_FIELD_NAMES = [
    *(f"bid{i}_price" for i in range(1, 6)),
    *(f"bid{i}_vol" for i in range(1, 6)),
    *(f"ask{i}_price" for i in range(1, 6)),
    *(f"ask{i}_vol" for i in range(1, 6)),
]
MICROSTRUCTURE_FIELD_NAMES = [
    "spread", "spread_pct", "bid_depth_vol", "ask_depth_vol",
    "bid_depth_amount", "ask_depth_amount", "depth_imbalance",
    "best_bid_amount", "best_ask_amount", "limit_seal_amount",
    "current_volume", "inside_volume", "outside_volume",
    "outside_inside_ratio", "active_net_volume", "speed_rate",
    "active1", "active2",
]
AUCTION_EXTRA_FIELD_NAMES = [
    "auction_unmatched_ratio", "auction_pressure_score",
]

_lock = threading.Lock()
_buffers: dict[str, list[dict]] = defaultdict(list)
_rings: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=RING_MAX_ROWS))
_last_flush: dict[str, float] = defaultdict(float)
_quality: dict[str, dict] = {}


def append_many(
    data_dir: Path,
    records: list[dict],
    *,
    source: str = "tdxapi",
    force_flush: bool = False,
) -> dict:
    """追加一批行情事实, 并按批次写入 parquet。

    返回本轮质量摘要。异常由调用方捕获; 本函数内部尽量只处理数据问题。
    """
    ingest_ts = int(time.time() * 1000)
    rows = [_normalize_record(r, source=source, ingest_ts=ingest_ts) for r in records]
    rows = [r for r in rows if r is not None]
    key = str(data_dir)
    now = time.monotonic()

    with _lock:
        ring = _rings[key]
        ring.extend(rows)
        _buffers[key].extend(rows)
        summary = _build_quality(rows, source=source, ingest_ts=ingest_ts)
        _quality[key] = summary
        should_flush = (
            force_flush
            or len(_buffers[key]) >= FLUSH_BATCH_SIZE
            or now - _last_flush[key] >= FLUSH_INTERVAL_S
        )
    if should_flush:
        flush(data_dir)
    return summary


def flush(data_dir: Path) -> int:
    """把内存批次落到 quote_ticks/date=YYYY-MM-DD/hour=HH/part-*.parquet。"""
    key = str(data_dir)
    with _lock:
        rows = _buffers.get(key, [])
        if not rows:
            return 0
        _buffers[key] = []
        _last_flush[key] = time.monotonic()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["trade_date"], row["hour"])].append(row)

    written = 0
    for (trade_date, hour), part_rows in grouped.items():
        target_dir = data_dir / "quote_ticks" / f"date={trade_date}" / f"hour={hour}"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"part-{int(time.time() * 1000)}-{id(part_rows)}.parquet"
        try:
            pl.DataFrame(part_rows).write_parquet(path)
            written += len(part_rows)
        except Exception as e:
            logger.warning("quote_ticks 写入失败(%s): %s", path, e)
            with _lock:
                _buffers[key] = part_rows + _buffers.get(key, [])
    return written


def latest(
    data_dir: Path,
    symbols: list[str] | None = None,
    *,
    target_date: date | None = None,
) -> list[dict]:
    """返回每个 symbol 最新一条 tick, 优先读热缓存, 不足再扫当天 parquet。"""
    wanted = {s.upper() for s in symbols or [] if s}
    rows = _recent_rows(data_dir, target_date=target_date or cn_today())
    if wanted:
        rows = [r for r in rows if str(r.get("symbol", "")).upper() in wanted]
    return _latest_by_symbol(rows)


def bars(
    data_dir: Path,
    symbol: str,
    *,
    freq: str = "5s",
    target_date: date | None = None,
) -> list[dict]:
    """从 quote_ticks 聚合 5s/1m/3m/5m/15m bar。"""
    target_date = target_date or cn_today()
    rows = [
        r for r in _recent_rows(data_dir, target_date=target_date)
        if str(r.get("symbol", "")).upper() == symbol.upper()
    ]
    if not rows:
        return []
    trade_rows = [r for r in rows if r.get("price_type") != "auction_reference"]
    if trade_rows:
        rows = trade_rows
    df = pl.DataFrame(rows)
    if "event_ts" not in df.columns or "last_price" not in df.columns:
        return []
    every = _freq_to_every(freq)
    try:
        df = (
            df.with_columns(pl.from_epoch("event_ts", time_unit="ms").alias("_dt"))
            .sort("_dt")
            .group_by_dynamic("_dt", every=every)
            .agg([
                pl.first("last_price").alias("open"),
                pl.max("last_price").alias("high"),
                pl.min("last_price").alias("low"),
                pl.last("last_price").alias("close"),
                (pl.last("volume") - pl.first("volume")).clip(0).alias("volume"),
                (pl.last("amount") - pl.first("amount")).clip(0).alias("amount"),
                pl.last("source").alias("source"),
                pl.last("ingest_ts").alias("ingest_ts"),
            ])
            .with_columns(pl.col("_dt").dt.strftime("%Y-%m-%dT%H:%M:%S").alias("datetime"))
            .select(["datetime", "open", "high", "low", "close", "volume", "amount", "source", "ingest_ts"])
        )
    except Exception as e:
        logger.warning("quote_ticks 聚合失败(%s, %s): %s", symbol, freq, e)
        return []
    return [_json_safe(row) for row in df.iter_rows(named=True)]


def quality(
    data_dir: Path,
    symbols: list[str] | None = None,
    *,
    target_date: date | None = None,
) -> dict:
    """返回数据质量摘要, 供决策台顶部状态展示。"""
    key = str(data_dir)
    latest_rows = latest(data_dir, symbols, target_date=target_date)
    now_ms = int(time.time() * 1000)
    missing = []
    if symbols:
        got = {r["symbol"] for r in latest_rows}
        missing = [s for s in symbols if s not in got]
    stale = [
        r["symbol"] for r in latest_rows
        if r.get("ingest_ts") and now_ms - int(r["ingest_ts"]) > STALE_MS
    ]
    base = dict(_quality.get(key) or {})
    base.update({
        "source": base.get("source") or "tdxapi",
        "symbol_count": len(latest_rows),
        "missing_symbols": missing,
        "stale_symbols": stale,
        "quote_freshness": _freshness(latest_rows, now_ms),
        "checked_at": now_ms,
    })
    return base


def read_ticks(
    data_dir: Path,
    *,
    target_date: date | None = None,
    symbols: list[str] | None = None,
) -> list[dict]:
    """读取某天 ticks, 用于 outcome 和盘中回放。"""
    rows = _recent_rows(data_dir, target_date=target_date or cn_today())
    wanted = {s.upper() for s in symbols or [] if s}
    if wanted:
        rows = [r for r in rows if str(r.get("symbol", "")).upper() in wanted]
    rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0))
    return rows


def _recent_rows(data_dir: Path, *, target_date: date) -> list[dict]:
    key = str(data_dir)
    ds = target_date.isoformat()
    with _lock:
        ring_rows = [dict(r) for r in _rings.get(key, []) if r.get("trade_date") == ds]
        buffered = [dict(r) for r in _buffers.get(key, []) if r.get("trade_date") == ds]
    disk_rows = _read_partition(data_dir, ds)
    return _dedupe_rows(disk_rows + ring_rows + buffered)


def _read_partition(data_dir: Path, ds: str) -> list[dict]:
    base = data_dir / "quote_ticks" / f"date={ds}"
    if not base.exists():
        return []
    paths = list(base.rglob("*.parquet"))
    if not paths:
        return []
    try:
        frames = [pl.read_parquet(str(p)) for p in paths]
        df = pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]
        return [
            _json_safe(row)
            for row in df.iter_rows(named=True)
        ]
    except Exception as e:
        logger.warning("quote_ticks 读取失败(%s): %s", base, e)
        return []


def _normalize_record(record: dict, *, source: str, ingest_ts: int) -> dict | None:
    symbol = str(record.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    last_price = _float_or_none(record.get("last_price") if record.get("last_price") is not None else record.get("close"))
    if last_price is None:
        return None
    event_ts = _event_ts_ms(record.get("timestamp")) or ingest_ts
    event_dt = datetime.fromtimestamp(event_ts / 1000, tz=CN_TZ)
    known_fields = {
        "symbol", "name", "last_price", "close", "prev_close", "open", "high", "low",
        "volume", "amount", "change_pct", "timestamp", "market_phase", "price_type",
        "auction_price", "auction_matched_volume", "auction_unmatched_side",
        "auction_unmatched_volume", "auction_change_pct", "bid1", "ask1", "bid1_vol",
        "ask1_vol", *DEPTH_FIELD_NAMES, *MICROSTRUCTURE_FIELD_NAMES,
        *AUCTION_EXTRA_FIELD_NAMES,
    }
    raw = {
        k: v for k, v in record.items()
        if k not in known_fields
    }
    bid1_price = _float_or_none(record.get("bid1_price") if record.get("bid1_price") is not None else record.get("bid1"))
    ask1_price = _float_or_none(record.get("ask1_price") if record.get("ask1_price") is not None else record.get("ask1"))
    out = {
        "symbol": symbol,
        "name": record.get("name"),
        "source": source,
        "event_ts": int(event_ts),
        "ingest_ts": int(ingest_ts),
        "trade_date": event_dt.date().isoformat(),
        "hour": f"{event_dt.hour:02d}",
        "last_price": last_price,
        "prev_close": _float_or_none(record.get("prev_close")),
        "open": _float_or_none(record.get("open")),
        "high": _float_or_none(record.get("high")),
        "low": _float_or_none(record.get("low")),
        "volume": _float_or_none(record.get("volume")),
        "amount": _float_or_none(record.get("amount")),
        "bid1": bid1_price,
        "ask1": ask1_price,
        "bid1_vol": _float_or_none(record.get("bid1_vol")),
        "ask1_vol": _float_or_none(record.get("ask1_vol")),
        "market_phase": record.get("market_phase"),
        "price_type": record.get("price_type") or "trade",
        "auction_price": _float_or_none(record.get("auction_price")),
        "auction_matched_volume": _float_or_none(record.get("auction_matched_volume")),
        "auction_unmatched_side": record.get("auction_unmatched_side"),
        "auction_unmatched_volume": _float_or_none(record.get("auction_unmatched_volume")),
        "auction_change_pct": _float_or_none(record.get("auction_change_pct")),
        "auction_unmatched_ratio": _float_or_none(record.get("auction_unmatched_ratio")),
        "auction_pressure_score": _float_or_none(record.get("auction_pressure_score")),
        "raw": json.dumps(raw, ensure_ascii=False) if raw else None,
    }
    for field in DEPTH_FIELD_NAMES:
        fallback = None
        if field == "bid1_price":
            fallback = bid1_price
        elif field == "ask1_price":
            fallback = ask1_price
        out[field] = _float_or_none(record.get(field)) if record.get(field) is not None else fallback
    for field in MICROSTRUCTURE_FIELD_NAMES:
        out[field] = _float_or_none(record.get(field))
    return out


def _event_ts_ms(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=CN_TZ)
        return int(dt.timestamp() * 1000)
    try:
        f = float(value)
        if f > 10_000_000_000:
            return int(f)
        if f > 1_000_000_000:
            return int(f * 1000)
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _latest_by_symbol(rows: list[dict]) -> list[dict]:
    keyed: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        prev = keyed.get(symbol)
        if prev is None or (row.get("event_ts") or 0, row.get("ingest_ts") or 0) >= (
            prev.get("event_ts") or 0,
            prev.get("ingest_ts") or 0,
        ):
            keyed[symbol] = row
    return [_json_safe(r) for r in sorted(keyed.values(), key=lambda r: r.get("symbol") or "")]


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    keyed: dict[tuple, dict] = {}
    for row in rows:
        key = (
            str(row.get("symbol") or ""),
            int(row.get("event_ts") or 0),
            int(row.get("ingest_ts") or 0),
            row.get("last_price"),
        )
        keyed[key] = row
    return list(keyed.values())


def _build_quality(rows: list[dict], *, source: str, ingest_ts: int) -> dict:
    symbols = [r["symbol"] for r in rows]
    duplicate_count = len(symbols) - len(set(symbols))
    lags = [
        ingest_ts - int(r["event_ts"]) for r in rows
        if r.get("event_ts") and ingest_ts >= int(r["event_ts"])
    ]
    return {
        "source": source,
        "source_latency_ms": max(lags) if lags else None,
        "ingest_lag_ms": sum(lags) / len(lags) if lags else None,
        "symbol_count": len(set(symbols)),
        "missing_symbols": [],
        "duplicate_count": duplicate_count,
        "stale_symbols": [],
        "fetch_ms": None,
        "flush_ms": None,
        "checked_at": ingest_ts,
    }


def _freshness(rows: list[dict], now_ms: int) -> str:
    if not rows:
        return "unknown"
    newest = max(int(r.get("ingest_ts") or 0) for r in rows)
    age = now_ms - newest
    if age <= STALE_MS:
        return "live"
    if age <= 5 * 60_000:
        return "stale"
    return "snapshot"


def _freq_to_every(freq: str) -> str:
    text = str(freq or "5s").lower()
    return {
        "5s": "5s",
        "10s": "10s",
        "30s": "30s",
        "1m": "1m",
        "3m": "3m",
        "5m": "5m",
        "15m": "15m",
    }.get(text, "5s")


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _json_safe(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            out[key] = value.isoformat()
        elif isinstance(value, date):
            out[key] = value.isoformat()
        elif value is not None and isinstance(value, float) and not math.isfinite(value):
            out[key] = None
        else:
            out[key] = value
    return out
