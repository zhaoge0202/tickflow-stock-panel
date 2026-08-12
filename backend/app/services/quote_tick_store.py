"""秒级行情事实层。

只记录系统从实时源看到的价格事实, 不表达买卖建议。当前决策台要求依赖
tdxapi, 因此 QuoteService 只会把 tdxapi 实时记录写入这里。

内存热缓存按 symbol 维护:
  - _latest: 每个 symbol 最新一条
  - _series: 关注标的最近 N 条 (供决策台盘中序列, 不再用全市场大 ring)
本地 parquet 是全市场回放事实层; 调用方用 series_symbols 收窄内存短序列。
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
from collections import defaultdict, deque
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.config import settings
from app.market_time import cn_today
from app.parquet import scan_parquet_compat

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
FLUSH_INTERVAL_S = 3.0
FLUSH_BATCH_SIZE = 5000
# 每个 symbol 热序列上限: 决策台分时/信号帧够用, 避免全市场 2 万行 Python dict ring。
SERIES_MAX_PER_SYMBOL = 300
STALE_MS = 15_000
# 无 symbol 条件的磁盘读取只允许读取最近文件，避免误把整天全市场快照物化。
UNSCOPED_READ_MAX_FILES = 16
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
TEXT_FIELD_NAMES = {
    "symbol", "name", "source", "trade_date", "hour", "market_phase",
    "price_type", "auction_unmatched_side", "event_time_quality", "raw",
}
INT_FIELD_NAMES = {"event_ts", "ingest_ts"}
FLOAT_FIELD_NAMES = {
    "last_price", "prev_close", "open", "high", "low", "volume", "amount",
    "change_pct", "change_amount", "amplitude", "turnover_rate",
    "bid1", "ask1", "bid1_vol", "ask1_vol", "auction_price",
    "auction_matched_volume", "auction_unmatched_volume", "auction_change_pct",
    *DEPTH_FIELD_NAMES, *MICROSTRUCTURE_FIELD_NAMES, *AUCTION_EXTRA_FIELD_NAMES,
}
QUOTE_TICK_SCHEMA_OVERRIDES = {
    **{field: pl.Utf8 for field in TEXT_FIELD_NAMES},
    **{field: pl.Int64 for field in INT_FIELD_NAMES},
    **{field: pl.Float64 for field in FLOAT_FIELD_NAMES},
}
MARKET_FRAME_SOURCE = "tdxapi_market_frame"
MINUTE_BACKFILL_SOURCE = "minute_backfill"
MARKET_REPLAY_SOURCES = {MARKET_FRAME_SOURCE, MINUTE_BACKFILL_SOURCE}
TIMELINE_MAX_POINTS = 2000

_lock = threading.Lock()
_buffers: dict[str, list[dict]] = defaultdict(list)
# data_dir -> symbol -> latest row
_latest: dict[str, dict[str, dict]] = defaultdict(dict)
# data_dir -> symbol -> recent rows (bounded)
_series: dict[str, dict[str, deque[dict]]] = defaultdict(
    lambda: defaultdict(lambda: deque(maxlen=SERIES_MAX_PER_SYMBOL))
)
_last_flush: dict[str, float] = defaultdict(float)
_quality: dict[str, dict] = {}


def append_many(
    data_dir: Path,
    records: list[dict],
    *,
    source: str = "tdxapi",
    force_flush: bool = False,
    series_symbols: set[str] | list[str] | None = None,
) -> dict:
    """追加一批行情事实, 并按批次写入 parquet。

    返回本轮质量摘要。异常由调用方捕获; 本函数内部尽量只处理数据问题。
    热缓存按 symbol 更新最新价; 有界短序列可用 series_symbols 收窄, 避免
    全市场回放落盘时把每只股票的 300 条短序列都堆在 Python 内存里。
    """
    ingest_ts = int(time.time() * 1000)
    rows = [_normalize_record(r, source=source, ingest_ts=ingest_ts) for r in records]
    rows = [r for r in rows if r is not None]
    key = str(data_dir)
    now = time.monotonic()
    keep_series = None
    if series_symbols is not None:
        keep_series = {str(s).strip().upper() for s in series_symbols if str(s).strip()}

    with _lock:
        latest_map = _latest[key]
        series_map = _series[key]
        for row in rows:
            symbol = row["symbol"]
            if keep_series is None or symbol in keep_series:
                series_map[symbol].append(row)
            prev = latest_map.get(symbol)
            if prev is None or (row.get("event_ts") or 0, row.get("ingest_ts") or 0) >= (
                prev.get("event_ts") or 0,
                prev.get("ingest_ts") or 0,
            ):
                latest_map[symbol] = row
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
            _quote_tick_frame(part_rows).write_parquet(path)
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
    target_date = target_date or cn_today()
    wanted = {s.upper() for s in symbols or [] if s}
    if wanted:
        hot_rows = [
            r for r in _hot_rows(data_dir, target_date=target_date)
            if str(r.get("symbol", "")).upper() in wanted
        ]
        if _symbols_covered(hot_rows, wanted):
            return _latest_by_symbol(hot_rows)
        mysql_rows = _mysql_latest(target_date=target_date, symbols=sorted(wanted))
        merged_rows = _dedupe_rows(mysql_rows + hot_rows)
        if _symbols_covered(merged_rows, wanted):
            return _latest_by_symbol(merged_rows)
        recent_rows = _read_recent_partition(
            data_dir,
            target_date.isoformat(),
            max_files=1200,
            symbols=wanted,
        )
        if _symbols_covered(recent_rows, wanted):
            return _latest_by_symbol(recent_rows)
    else:
        # 进程重启后内存环为空时，从 MySQL 热表恢复最新行情；
        # 这张表只有每个 symbol 一行，不会扫描 quote_ticks 历史。
        mysql_rows = _mysql_latest(target_date=target_date)
        if mysql_rows:
            return _latest_by_symbol(mysql_rows)
    rows = _recent_rows(
        data_dir,
        target_date=target_date,
        symbols=wanted or None,
    )
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
        r for r in _hot_rows(data_dir, target_date=target_date)
        if str(r.get("symbol", "")).upper() == symbol.upper()
    ]
    if not rows:
        rows = _read_recent_partition(
            data_dir,
            target_date.isoformat(),
            max_files=1200,
            symbols={symbol.upper()},
        )
    if not rows:
        rows = _recent_rows(
            data_dir,
            target_date=target_date,
            symbols={symbol.upper()},
        )
    if not rows:
        return []
    return _bars_from_rows(rows, symbol, freq)


def _bars_from_rows(rows: list[dict], symbol: str, freq: str) -> list[dict]:
    """从已过滤的单标的 tick 聚合 bar, 避免详情页反复扫全量分区。"""
    rows = [
        r for r in rows
        if str(r.get("symbol", "")).upper() == symbol.upper()
    ]
    trade_rows = [r for r in rows if r.get("price_type") != "auction_reference"]
    if trade_rows:
        rows = trade_rows
    df = _quote_tick_frame(rows)
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
    prefer_hot: bool = False,
) -> list[dict]:
    """读取某天 ticks, 用于 outcome 和盘中回放。"""
    target_date = target_date or cn_today()
    wanted = {s.upper() for s in symbols or [] if s}
    if prefer_hot and wanted:
        hot_rows = [
            r for r in _hot_rows(data_dir, target_date=target_date)
            if str(r.get("symbol", "")).upper() in wanted
        ]
        if _symbols_covered(hot_rows, wanted):
            hot_rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0))
            return hot_rows
        recent_rows = _read_recent_partition(
            data_dir,
            target_date.isoformat(),
            max_files=1200,
            symbols=wanted,
        )
        if _symbols_covered(recent_rows, wanted):
            recent_rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0))
            return recent_rows
    rows = _recent_rows(
        data_dir,
        target_date=target_date,
        symbols=wanted or None,
    )
    rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0))
    return rows


def read_all_ticks(
    data_dir: Path,
    *,
    target_date: date | None = None,
    symbols: list[str] | None = None,
) -> list[dict]:
    """读取某天完整 quote_ticks 分区。

    仅供全市场回放/时间线使用。普通详情页仍应传 symbols 走 read_ticks。
    """
    target_date = target_date or cn_today()
    wanted = {s.upper() for s in symbols or [] if s}
    rows = _read_partition(
        data_dir,
        target_date.isoformat(),
        symbols=wanted or None,
    )
    hot_rows = _hot_rows(data_dir, target_date=target_date)
    if wanted:
        hot_rows = [
            row for row in hot_rows
            if str(row.get("symbol", "")).upper() in wanted
        ]
    rows = _dedupe_rows(rows + hot_rows)
    rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0))
    return rows


def read_sampled_ticks(
    data_dir: Path,
    *,
    target_date: date | None = None,
    symbols: list[str] | None = None,
    step_seconds: int = 60,
    max_files: int | None = None,
) -> list[dict]:
    """按固定时间粒度读取某天 tick 序列。

    面向板块曲线这类多标的聚合场景：读取阶段先把同一 symbol 在同一
    时间桶内压成最后一条，避免把全天全市场原始 tick 全部物化成 Python dict。
    """
    target_date = target_date or cn_today()
    wanted = {str(s).strip().upper() for s in symbols or [] if str(s).strip()}
    ds = target_date.isoformat()
    base = data_dir / "quote_ticks" / f"date={ds}"
    disk_rows: list[dict] = []
    if base.exists():
        paths = (
            _recent_partition_paths(base, max_files=max_files)
            if max_files is not None
            else sorted(base.rglob("*.parquet"))
        )
        disk_rows = _read_sampled_parquet_paths(
            paths,
            base,
            symbols=wanted or None,
            step_seconds=step_seconds,
        )

    hot_rows = _hot_rows(data_dir, target_date=target_date)
    if wanted:
        hot_rows = [
            row for row in hot_rows
            if str(row.get("symbol", "")).upper() in wanted
        ]
    rows = _sample_tick_rows(disk_rows + hot_rows, step_seconds=step_seconds)
    rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("symbol") or "", r.get("ingest_ts") or 0))
    return rows


def event_timestamps(
    data_dir: Path,
    *,
    target_date: date | None = None,
) -> list[int]:
    """返回某天全部 event_ts 去重列表, 用于全市场回放时间线。"""
    target_date = target_date or cn_today()
    ds = target_date.isoformat()
    base = data_dir / "quote_ticks" / f"date={ds}"
    timestamps: set[int] = set()
    if base.exists():
        paths = sorted(base.rglob("*.parquet"))
        if paths:
            try:
                frame = scan_parquet_compat(
                    [str(path) for path in paths],
                    schema=QUOTE_TICK_SCHEMA_OVERRIDES,
                    hive_partitioning=False,
                    cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
                )
                df = (
                    frame
                    .select("event_ts")
                    .filter(pl.col("event_ts").is_not_null())
                    .unique()
                    .collect(engine="streaming")
                )
                timestamps.update(int(v) for v in df["event_ts"].to_list() if v is not None)
            except Exception as e:  # noqa: BLE001
                logger.warning("quote_ticks 时间线读取失败(%s): %s", base, e)
    for row in _hot_rows(data_dir, target_date=target_date):
        ts = row.get("event_ts")
        if ts is not None:
            timestamps.add(int(ts))
    return sorted(timestamps)


def timeline_points(
    data_dir: Path,
    *,
    target_date: date | None = None,
    step_seconds: int = 60,
) -> dict:
    """返回某日可回放时间线摘要。

    points 仍按固定步长生成，避免把全市场分钟帧的全部原始 event_ts 暴露给前端。
    symbol_count/sources 用于判断旧分区是否只是关注标的稀疏 tick。
    """
    target_date = target_date or cn_today()
    event_ts = event_timestamps(data_dir, target_date=target_date)
    symbols, sources = _partition_symbols_sources(data_dir, target_date=target_date)
    if not event_ts:
        return {
            "points": [],
            "start_ts": None,
            "end_ts": None,
            "has_ticks": False,
            "symbol_count": len(symbols),
            "sources": sorted(sources),
            "count": 0,
        }

    start_ts = event_ts[0]
    end_ts = event_ts[-1]
    step_ms = max(30, int(step_seconds)) * 1000
    points: list[int] = []
    cur = start_ts
    while cur <= end_ts and len(points) < TIMELINE_MAX_POINTS:
        points.append(cur)
        cur += step_ms
    if (not points or points[-1] != end_ts) and len(points) < TIMELINE_MAX_POINTS:
        points.append(end_ts)
    return {
        "points": points,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "has_ticks": True,
        "symbol_count": len(symbols),
        "sources": sorted(sources),
        "count": len(points),
    }


def materialize_from_minute(
    data_dir: Path,
    *,
    target_date: date | None = None,
    force: bool = False,
) -> dict:
    """用本地 1m K 线补出板块回放用的全市场分钟帧。

    不访问外部 provider；若本地没有 kline_minute，则返回 missing_minute。
    """
    target_date = target_date or cn_today()
    ds = target_date.isoformat()
    base = data_dir / "quote_ticks" / f"date={ds}"
    if not force and _partition_has_source(base, MINUTE_BACKFILL_SOURCE):
        return {"status": "exists", "date": ds, "rows": 0, "symbols": 0, "hours": 0}

    minute_path = data_dir / "kline_minute" / f"date={ds}" / "part.parquet"
    if not minute_path.exists():
        return {"status": "missing_minute", "date": ds, "rows": 0, "symbols": 0, "hours": 0}

    try:
        minute_df = pl.read_parquet(minute_path)
        tick_df = _minute_frame_to_quote_ticks(data_dir, target_date, minute_df)
    except Exception as exc:  # noqa: BLE001
        logger.warning("minute replay backfill failed(%s): %s", minute_path, exc)
        return {"status": "failed", "date": ds, "rows": 0, "symbols": 0, "hours": 0, "error": str(exc)}
    if tick_df.is_empty():
        return {"status": "empty_minute", "date": ds, "rows": 0, "symbols": 0, "hours": 0}

    written = 0
    hours = 0
    symbols = set(tick_df["symbol"].to_list()) if "symbol" in tick_df.columns else set()
    for hour_df in tick_df.partition_by("hour"):
        hour = str(hour_df["hour"][0])
        target_dir = base / f"hour={hour}"
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / f"part-minute-backfill-{ds}-{hour}.parquet"
        tmp_path = target_dir / f".minute-backfill-{int(time.time() * 1000)}.parquet"
        try:
            hour_df.write_parquet(tmp_path)
            tmp_path.replace(final_path)
            written += hour_df.height
            hours += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("minute replay backfill write failed(%s): %s", final_path, exc)
            with suppress(OSError):
                tmp_path.unlink()

    logger.info(
        "minute replay backfill complete: date=%s rows=%d symbols=%d hours=%d",
        ds, written, len(symbols), hours,
    )
    return {"status": "materialized", "date": ds, "rows": written, "symbols": len(symbols), "hours": hours}


def snapshot_as_of(
    data_dir: Path,
    *,
    target_date: date | None = None,
    as_of_ts: int | None = None,
) -> tuple[list[dict], int | None]:
    """读取某日截止 as_of_ts 的每 symbol 最新一条。

    与 read_ticks 不同, 这里面向全市场回放, 用 Polars 流式扫描分区后在
    DataFrame 内完成 last-by-symbol, 避免把全天所有 tick 先物化成 Python dict。
    """
    target_date = target_date or cn_today()
    ds = target_date.isoformat()
    base = data_dir / "quote_ticks" / f"date={ds}"
    frames: list[pl.DataFrame] = []
    if base.exists():
        paths = sorted(base.rglob("*.parquet"))
        if paths:
            try:
                frame = scan_parquet_compat(
                    [str(path) for path in paths],
                    schema=QUOTE_TICK_SCHEMA_OVERRIDES,
                    hive_partitioning=False,
                    cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
                )
                if as_of_ts is not None:
                    frame = frame.filter(pl.col("event_ts") <= int(as_of_ts))
                frames.append(
                    frame
                    .sort(["symbol", "event_ts", "ingest_ts"])
                    .unique(subset=["symbol"], keep="last")
                    .collect(engine="streaming")
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("quote_ticks 快照读取失败(%s): %s", base, e)
    hot_rows = _hot_rows(data_dir, target_date=target_date)
    if as_of_ts is not None:
        hot_rows = [
            row for row in hot_rows
            if (row.get("event_ts") or 0) <= int(as_of_ts)
        ]
    if hot_rows:
        frames.append(_quote_tick_frame(hot_rows))
    if not frames:
        return [], None
    try:
        df = pl.concat(frames, how="diagonal_relaxed")
        if df.is_empty() or "symbol" not in df.columns or "event_ts" not in df.columns:
            return [], None
        df = df.sort(["symbol", "event_ts", "ingest_ts"]).unique(subset=["symbol"], keep="last")
        actual_ts = int(df["event_ts"].max())
        return [_json_safe(row) for row in df.iter_rows(named=True)], actual_ts
    except Exception as e:  # noqa: BLE001
        logger.warning("quote_ticks 快照合并失败(%s): %s", base, e)
        return [], None


def _recent_rows(
    data_dir: Path,
    *,
    target_date: date,
    symbols: set[str] | None = None,
) -> list[dict]:
    hot_rows = _hot_rows(data_dir, target_date=target_date)
    if symbols:
        hot_rows = [
            row for row in hot_rows
            if str(row.get("symbol", "")).upper() in symbols
        ]
    if symbols:
        disk_rows = _read_partition(
            data_dir,
            target_date.isoformat(),
            symbols=symbols,
        )
    else:
        # 无条件的全市场读取仅作为无 MySQL 配置时的有限降级，
        # 不允许回到“整天所有文件 + Python dict”模式。
        disk_rows = _read_recent_partition(
            data_dir,
            target_date.isoformat(),
            max_files=UNSCOPED_READ_MAX_FILES,
        )
    return _dedupe_rows(disk_rows + hot_rows)


def _hot_rows(data_dir: Path, *, target_date: date) -> list[dict]:
    """热路径: 每 symbol 有界短序列 + 未 flush buffer。"""
    key = str(data_dir)
    ds = target_date.isoformat()
    with _lock:
        series_rows: list[dict] = []
        for rows in _series.get(key, {}).values():
            series_rows.extend(dict(r) for r in rows if r.get("trade_date") == ds)
        # latest 兜底: 序列因跨日清空后仍能返回当日最新一条
        for row in _latest.get(key, {}).values():
            if row.get("trade_date") == ds:
                series_rows.append(dict(row))
        buffered = [dict(r) for r in _buffers.get(key, []) if r.get("trade_date") == ds]
    return _dedupe_rows(series_rows + buffered)


def cleanup_old_partitions(
    data_dir: Path,
    *,
    keep_days: int | None = None,
) -> dict:
    """删除过期/非法 quote_ticks 分区, 控制磁盘膨胀。

    keep_days 默认取 settings.quote_ticks_retention_days。
    非法日期 (解析失败 / 明显越界如 1970、2263) 一并清理。
    """
    retention = settings.quote_ticks_retention_days if keep_days is None else int(keep_days)
    retention = max(1, retention)
    base = data_dir / "quote_ticks"
    if not base.exists():
        return {"removed": [], "kept": [], "retention_days": retention}

    today = cn_today()
    cutoff = today - timedelta(days=retention - 1)
    removed: list[str] = []
    kept: list[str] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir() or not path.name.startswith("date="):
            continue
        ds = path.name[5:]
        drop = False
        try:
            day = date.fromisoformat(ds)
        except ValueError:
            drop = True
        else:
            # 脏分区 / 过期分区
            if day.year < 1990 or day.year > today.year + 1 or day < cutoff:
                drop = True
        if drop:
            try:
                shutil.rmtree(path)
                removed.append(ds)
            except OSError as exc:
                logger.warning("quote_ticks 清理失败(%s): %s", path, exc)
        else:
            kept.append(ds)

    # 顺手丢掉非今日的内存热缓存, 防止跨日残留占内存
    key = str(data_dir)
    today_ds = today.isoformat()
    with _lock:
        latest_map = _latest.get(key)
        if latest_map:
            stale_symbols = [
                symbol for symbol, row in latest_map.items()
                if row.get("trade_date") != today_ds
            ]
            for symbol in stale_symbols:
                latest_map.pop(symbol, None)
                series = _series.get(key, {})
                series.pop(symbol, None)

    if removed:
        logger.info(
            "quote_ticks 清理完成: removed=%d kept=%d retention_days=%d",
            len(removed),
            len(kept),
            retention,
        )
    return {"removed": removed, "kept": kept, "retention_days": retention}


def compact_partition(
    data_dir: Path,
    *,
    target_date: date | None = None,
    symbols: set[str] | list[str] | None = None,
) -> dict:
    """把某日 quote_ticks 压成「仅关注标的 + 每小时一个文件」。

    用于收窄写入策略上线前已经落盘的全市场残量。symbols 为空时只合并文件不筛标的。
    压缩过程用临时目录 + 原子替换, 避免半写状态。
    """
    target_date = target_date or cn_today()
    ds = target_date.isoformat()
    base = data_dir / "quote_ticks" / f"date={ds}"
    if not base.exists():
        return {"date": ds, "hours": 0, "rows": 0, "symbols": 0, "removed_files": 0}

    wanted = {str(s).strip().upper() for s in (symbols or []) if s}
    hour_dirs = sorted(p for p in base.iterdir() if p.is_dir() and p.name.startswith("hour="))
    total_rows = 0
    kept_symbols: set[str] = set()
    removed_files = 0
    written_hours = 0

    for hour_dir in hour_dirs:
        paths = sorted(hour_dir.glob("*.parquet"))
        if not paths:
            continue
        try:
            frame = scan_parquet_compat(
                [str(path) for path in paths],
                schema=QUOTE_TICK_SCHEMA_OVERRIDES,
                hive_partitioning=False,
                cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
            )
            if wanted:
                frame = frame.filter(pl.col("symbol").is_in(sorted(wanted)))
            df = frame.collect(engine="streaming")
        except Exception as exc:  # noqa: BLE001
            logger.warning("quote_ticks 压缩读取失败(%s): %s", hour_dir, exc)
            continue

        # 先写临时文件, 成功后再删旧 part, 最后 rename
        tmp_path = hour_dir / f".compact-{int(time.time() * 1000)}.parquet"
        final_path = hour_dir / f"part-compact-{ds}-{hour_dir.name[5:]}.parquet"
        try:
            if df.is_empty():
                # 该小时没有关注标的: 清空整个 hour 目录
                for path in paths:
                    try:
                        path.unlink()
                        removed_files += 1
                    except OSError:
                        pass
                try:
                    hour_dir.rmdir()
                except OSError:
                    pass
                continue

            df.write_parquet(tmp_path)
            for path in paths:
                if path.resolve() == tmp_path.resolve():
                    continue
                try:
                    path.unlink()
                    removed_files += 1
                except OSError as exc:
                    logger.warning("quote_ticks 压缩删旧失败(%s): %s", path, exc)
            tmp_path.replace(final_path)
            total_rows += len(df)
            if "symbol" in df.columns:
                kept_symbols.update(str(s).upper() for s in df["symbol"].unique().to_list())
            written_hours += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("quote_ticks 压缩写入失败(%s): %s", hour_dir, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    result = {
        "date": ds,
        "hours": written_hours,
        "rows": total_rows,
        "symbols": len(kept_symbols),
        "removed_files": removed_files,
    }
    logger.info(
        "quote_ticks 压缩完成: date=%s hours=%d rows=%d symbols=%d removed_files=%d",
        ds, written_hours, total_rows, len(kept_symbols), removed_files,
    )
    return result


def _partition_symbols_sources(data_dir: Path, *, target_date: date) -> tuple[set[str], set[str]]:
    ds = target_date.isoformat()
    base = data_dir / "quote_ticks" / f"date={ds}"
    symbols: set[str] = set()
    sources: set[str] = set()
    paths = sorted(base.rglob("*.parquet")) if base.exists() else []
    if paths:
        try:
            frame = scan_parquet_compat(
                [str(path) for path in paths],
                schema=QUOTE_TICK_SCHEMA_OVERRIDES,
                hive_partitioning=False,
                cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
            )
            schema_names = set(frame.collect_schema().names())
            if "symbol" in schema_names:
                symbol_df = frame.select(pl.col("symbol").drop_nulls().unique()).collect()
                symbols.update(
                    str(s).strip().upper()
                    for s in symbol_df["symbol"].to_list()
                    if str(s).strip()
                )
            if "source" in schema_names:
                source_df = frame.select(pl.col("source").drop_nulls().unique()).collect()
                sources.update(
                    str(s).strip()
                    for s in source_df["source"].to_list()
                    if str(s).strip()
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("quote_ticks metadata read failed(%s): %s", base, exc)

    for row in _hot_rows(data_dir, target_date=target_date):
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            symbols.add(symbol)
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
    return symbols, sources


def _partition_has_source(base: Path, source: str) -> bool:
    if not base.exists():
        return False
    paths = sorted(base.rglob("*.parquet"))
    if not paths:
        return False
    try:
        frame = scan_parquet_compat(
            [str(path) for path in paths],
            schema=QUOTE_TICK_SCHEMA_OVERRIDES,
            hive_partitioning=False,
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
        if "source" not in frame.collect_schema().names():
            return False
        result = frame.filter(pl.col("source") == source).select(pl.len().alias("n")).collect()
        return int(result["n"][0] or 0) > 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("quote_ticks source check failed(%s): %s", base, exc)
        return False


def _minute_frame_to_quote_ticks(data_dir: Path, target_date: date, df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or not {"symbol", "datetime", "close"}.issubset(df.columns):
        return pl.DataFrame()
    ds = target_date.isoformat()
    keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
    df = df.select(keep).filter(
        pl.col("symbol").is_not_null()
        & pl.col("datetime").is_not_null()
        & pl.col("close").is_not_null()
    )
    if df.is_empty():
        return pl.DataFrame()

    dt_type = df.schema["datetime"]
    if isinstance(dt_type, pl.Datetime):
        dt_expr = (
            pl.col("datetime").dt.convert_time_zone("Asia/Shanghai")
            if dt_type.time_zone
            else pl.col("datetime").dt.replace_time_zone("Asia/Shanghai")
        )
    else:
        dt_expr = pl.col("datetime").cast(pl.Utf8).str.to_datetime(strict=False).dt.replace_time_zone("Asia/Shanghai")

    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
        else:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    ingest_ts = int(time.time() * 1000)
    df = (
        df.with_columns([
            pl.col("symbol").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
            dt_expr.alias("_dt_cn"),
        ])
        .filter(pl.col("symbol") != "")
        .with_columns([
            pl.col("_dt_cn").dt.timestamp("ms").alias("event_ts"),
            pl.col("_dt_cn").dt.strftime("%H").alias("hour"),
        ])
        .filter(pl.col("event_ts").is_not_null())
        .sort(["symbol", "event_ts"])
    )

    prev_close = _prev_close_frame(data_dir, target_date)
    if prev_close is not None and not prev_close.is_empty():
        df = df.join(prev_close, on="symbol", how="left")
    else:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("prev_close"))

    df = df.with_columns([
        pl.first("open").over("symbol").alias("open"),
        pl.col("high").cum_max().over("symbol").alias("high"),
        pl.col("low").cum_min().over("symbol").alias("low"),
        pl.col("volume").fill_null(0).cum_sum().over("symbol").alias("volume"),
        pl.col("amount").fill_null(0).cum_sum().over("symbol").alias("amount"),
        pl.when(pl.col("prev_close") > 0)
        .then(pl.col("close") / pl.col("prev_close") - 1)
        .otherwise(None)
        .alias("change_pct"),
    ])
    return df.select([
        pl.col("symbol"),
        pl.lit(None, dtype=pl.Utf8).alias("name"),
        pl.lit(MINUTE_BACKFILL_SOURCE).alias("source"),
        pl.col("event_ts").cast(pl.Int64),
        pl.lit(ingest_ts).cast(pl.Int64).alias("ingest_ts"),
        pl.lit(ds).alias("trade_date"),
        pl.col("hour"),
        pl.col("close").alias("last_price"),
        pl.col("prev_close"),
        pl.col("open"),
        pl.col("high"),
        pl.col("low"),
        pl.col("volume"),
        pl.col("amount"),
        pl.col("change_pct"),
        pl.lit(None, dtype=pl.Float64).alias("change_amount"),
        pl.lit(None, dtype=pl.Float64).alias("amplitude"),
        pl.lit(None, dtype=pl.Float64).alias("turnover_rate"),
        pl.lit("minute_close").alias("price_type"),
    ])


def _prev_close_frame(data_dir: Path, target_date: date) -> pl.DataFrame | None:
    base = data_dir / "kline_daily"
    if not base.exists():
        return None
    prev_dates: list[date] = []
    for path in base.iterdir():
        if not path.is_dir() or not path.name.startswith("date="):
            continue
        try:
            day = date.fromisoformat(path.name[5:])
        except ValueError:
            continue
        if day < target_date:
            prev_dates.append(day)
    for day in sorted(prev_dates, reverse=True):
        path = base / f"date={day.isoformat()}" / "part.parquet"
        if not path.exists():
            continue
        try:
            return pl.read_parquet(path, columns=["symbol", "close"]).select([
                pl.col("symbol").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
                pl.col("close").cast(pl.Float64, strict=False).alias("prev_close"),
            ]).unique(subset=["symbol"], keep="last")
        except Exception as exc:  # noqa: BLE001
            logger.debug("prev close read skipped(%s): %s", path, exc)
    return None


def _symbols_covered(rows: list[dict], wanted: set[str]) -> bool:
    if not wanted:
        return bool(rows)
    got = {str(r.get("symbol", "")).upper() for r in rows}
    return wanted.issubset(got)


def _read_recent_partition(
    data_dir: Path,
    ds: str,
    *,
    max_files: int,
    symbols: set[str] | None = None,
) -> list[dict]:
    base = data_dir / "quote_ticks" / f"date={ds}"
    if not base.exists():
        return []
    paths = _recent_partition_paths(base, max_files=max_files)
    return _read_parquet_paths(paths, base, symbols=symbols)


def _recent_partition_paths(base: Path, *, max_files: int) -> list[Path]:
    paths = []
    for path in base.rglob("*.parquet"):
        try:
            stat = path.stat()
        except OSError:
            continue
        paths.append((stat.st_mtime_ns, path))
    paths.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in paths[:max_files]]


def _read_partition(
    data_dir: Path,
    ds: str,
    *,
    symbols: set[str] | None = None,
) -> list[dict]:
    base = data_dir / "quote_ticks" / f"date={ds}"
    if not base.exists():
        return []
    paths = sorted(base.rglob("*.parquet"))
    if not paths:
        return []
    return _read_parquet_paths(paths, base, symbols=symbols)


def _read_parquet_paths(
    paths: list[Path],
    base: Path,
    *,
    symbols: set[str] | None = None,
) -> list[dict]:
    if not paths:
        return []
    wanted = sorted(symbols or set())
    try:
        frame = scan_parquet_compat(
            [str(path) for path in paths],
            schema=QUOTE_TICK_SCHEMA_OVERRIDES,
            hive_partitioning=False,
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
        if wanted:
            frame = frame.filter(pl.col("symbol").is_in(wanted))
        df = frame.collect(engine="streaming")
        return [_json_safe(row) for row in df.iter_rows(named=True)]
    except Exception as e:
        logger.warning("quote_ticks 合并失败(%s): %s", base, e)
        return []


def _read_sampled_parquet_paths(
    paths: list[Path],
    base: Path,
    *,
    symbols: set[str] | None = None,
    step_seconds: int = 60,
) -> list[dict]:
    if not paths:
        return []
    wanted = sorted(symbols or set())
    step_ms = max(30, int(step_seconds or 60)) * 1000
    keep = [
        "symbol", "event_ts", "ingest_ts", "last_price", "prev_close", "change_pct", "amount",
        "outside_volume", "inside_volume", "active_net_volume", "source",
    ]
    try:
        frame = scan_parquet_compat(
            [str(path) for path in paths],
            schema=QUOTE_TICK_SCHEMA_OVERRIDES,
            hive_partitioning=False,
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
        schema_names = set(frame.collect_schema().names())
        if not {"symbol", "event_ts"}.issubset(schema_names):
            return []
        for column in keep:
            if column not in schema_names:
                dtype = pl.Utf8 if column in {"symbol", "source"} else pl.Float64
                frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
        frame = (
            frame.select(keep)
            .filter(pl.col("symbol").is_not_null() & pl.col("event_ts").is_not_null())
            .with_columns([
                pl.col("symbol").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
                pl.col("event_ts").cast(pl.Int64, strict=False).alias("event_ts"),
                pl.col("ingest_ts").cast(pl.Int64, strict=False).alias("ingest_ts"),
            ])
            .filter(pl.col("symbol") != "")
        )
        if wanted:
            frame = frame.filter(pl.col("symbol").is_in(wanted))
        sampled = (
            frame
            .with_columns((pl.col("event_ts") // step_ms * step_ms).alias("_bucket_ts"))
            .sort(["symbol", "_bucket_ts", "event_ts", "ingest_ts"])
            .unique(subset=["symbol", "_bucket_ts"], keep="last")
            .with_columns(pl.col("_bucket_ts").alias("event_ts"))
            .drop("_bucket_ts")
            .collect(engine="streaming")
        )
        return [_json_safe(row) for row in sampled.iter_rows(named=True)]
    except Exception as e:
        logger.warning("quote_ticks 采样读取失败(%s): %s", base, e)
        return []


def _sample_tick_rows(rows: list[dict], *, step_seconds: int = 60) -> list[dict]:
    if not rows:
        return []
    step_ms = max(30, int(step_seconds or 60)) * 1000
    latest: dict[tuple[str, int], dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        event_ts = row.get("event_ts")
        if not symbol or event_ts is None:
            continue
        try:
            ts = int(event_ts)
        except (TypeError, ValueError):
            continue
        bucket = ts // step_ms * step_ms
        key = (symbol, bucket)
        prev = latest.get(key)
        rank = (ts, int(row.get("ingest_ts") or 0))
        prev_rank = (
            int(prev.get("event_ts") or 0),
            int(prev.get("ingest_ts") or 0),
        ) if prev is not None else None
        if prev is None or rank >= prev_rank:
            item = dict(row)
            item["symbol"] = symbol
            item["event_ts"] = bucket
            latest[key] = item
    return list(latest.values())


def _mysql_latest(
    *,
    target_date: date,
    symbols: list[str] | None = None,
) -> list[dict]:
    """读取 MySQL 最新快照；不可用时返回空并让调用方走本地降级。"""
    try:
        from app.services.quote_snapshot_mysql import quote_snapshot_mysql_store

        if target_date != cn_today() or not quote_snapshot_mysql_store.enabled():
            return []
        return quote_snapshot_mysql_store.list(symbols=symbols, trade_date=target_date)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quote_latest MySQL 读取失败, 使用本地 tick: %s", exc)
        return []


def _quote_tick_frame(rows: list[dict]) -> pl.DataFrame:
    """用固定 schema 构造 quote_ticks DataFrame, 避免全市场批次类型推断漂移。"""
    return pl.DataFrame(
        rows,
        schema_overrides=QUOTE_TICK_SCHEMA_OVERRIDES,
        infer_schema_length=None,
    )


def _normalize_record(record: dict, *, source: str, ingest_ts: int) -> dict | None:
    symbol = str(record.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    last_price = _float_or_none(record.get("last_price") if record.get("last_price") is not None else record.get("close"))
    if last_price is None:
        return None
    event_ts = _event_ts_ms(record.get("timestamp")) or _event_ts_ms(record.get("event_ts")) or ingest_ts
    event_dt = datetime.fromtimestamp(event_ts / 1000, tz=CN_TZ)
    known_fields = {
        "symbol", "name", "last_price", "close", "prev_close", "open", "high", "low",
        "volume", "amount", "change_pct", "change_amount", "amplitude", "turnover_rate",
        "timestamp", "event_ts", "ingest_ts", "trade_date", "hour", "source", "raw",
        "market_phase", "price_type",
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
        "change_pct": _float_or_none(record.get("change_pct")),
        "change_amount": _float_or_none(record.get("change_amount")),
        "amplitude": _float_or_none(record.get("amplitude")),
        "turnover_rate": _float_or_none(record.get("turnover_rate")),
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
