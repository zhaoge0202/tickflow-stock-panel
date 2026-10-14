"""TDX 市场广度快照。

市场广度只作为决策台环境提示和风险解释, 不参与自动交易。
"""
from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.market_time import cn_today
from app.plugins.tdxapi.provider import TDXAPIProvider

_CACHE_TTL_S = 10.0
_DISK_TTL_S = 60.0
CN_TZ = ZoneInfo("Asia/Shanghai")
_cache: dict | None = None
_cache_at = 0.0


def cached(data_dir: Path | None = None) -> dict:
    """读取已缓存或已落盘的市场广度, 不主动请求 sidecar。"""
    if _cache is not None:
        return dict(_cache)
    if data_dir is not None:
        disk = read_latest(data_dir)
        if disk:
            return dict(disk)
    return unavailable()


def latest(data_dir: Path | None = None, *, force: bool = False, persist: bool = True) -> dict:
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and now - _cache_at <= _CACHE_TTL_S:
        return dict(_cache)
    if not force and data_dir is not None:
        disk = read_latest(data_dir)
        if disk and _snapshot_age_s(disk) <= _DISK_TTL_S:
            _cache = disk
            _cache_at = now
            return dict(disk)

    provider = TDXAPIProvider()
    try:
        snapshot = provider.get_market_breadth()
    finally:
        provider.close()
    if data_dir is not None and persist:
        append(data_dir, snapshot)
    _cache = snapshot
    _cache_at = now
    return dict(snapshot)


def safe_latest(data_dir: Path | None = None, *, force: bool = False, persist: bool = True) -> dict:
    try:
        return latest(data_dir, force=force, persist=persist)
    except Exception as e:  # noqa: BLE001
        if data_dir is not None:
            disk = read_latest(data_dir)
            if disk:
                return {**disk, "status": "stale_fallback", "error": str(e)}
        return unavailable(str(e))


def unavailable(error: str | None = None) -> dict:
    out = {
        "source": "tdxapi",
        "status": "unavailable",
        "up_count": None,
        "down_count": None,
        "flat_count": None,
        "total_count": None,
        "up_down_ratio": None,
        "market_temperature": "unknown",
        "major_index_change_pct": None,
        "major_indices": [],
        "exchanges": {},
    }
    if error:
        out["error"] = error
    return out


def append(data_dir: Path, snapshot: dict) -> None:
    row = _to_row(snapshot)
    trade_date = row["trade_date"]
    target_dir = data_dir / "market_breadth" / f"date={trade_date}"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"part-{int(time.time() * 1000)}.parquet"
    pl.DataFrame([row]).write_parquet(path)


def read_latest(data_dir: Path, *, target_date: date | None = None) -> dict | None:
    ds = (target_date or cn_today()).isoformat()
    base = data_dir / "market_breadth" / f"date={ds}"
    if not base.exists():
        return None
    paths = list(base.rglob("*.parquet"))
    if not paths:
        return None
    try:
        frames = [pl.read_parquet(str(p)) for p in paths]
        df = pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]
    except Exception:
        return None
    if df.is_empty():
        return None
    row = df.sort("ingest_ts").tail(1).to_dicts()[0]
    return _from_row(row)


def _to_row(snapshot: dict) -> dict:
    event_ts = _int_or_now(snapshot.get("event_ts"))
    ingest_ts = _int_or_now(snapshot.get("ingest_ts"))
    event_dt = datetime.fromtimestamp(event_ts / 1000, tz=CN_TZ)
    return {
        "source": snapshot.get("source") or "tdxapi",
        "status": snapshot.get("status"),
        "event_ts": event_ts,
        "ingest_ts": ingest_ts,
        "trade_date": event_dt.date().isoformat(),
        "up_count": _num(snapshot.get("up_count")),
        "down_count": _num(snapshot.get("down_count")),
        "flat_count": _num(snapshot.get("flat_count")),
        "total_count": _num(snapshot.get("total_count")),
        "up_down_ratio": _num(snapshot.get("up_down_ratio")),
        "market_temperature": snapshot.get("market_temperature") or "unknown",
        "major_index_change_pct": _num(snapshot.get("major_index_change_pct")),
        "major_indices": json.dumps(snapshot.get("major_indices") or [], ensure_ascii=False),
        "exchanges": json.dumps(snapshot.get("exchanges") or {}, ensure_ascii=False),
        "raw": json.dumps(snapshot.get("raw") or {}, ensure_ascii=False, default=str),
    }


def _from_row(row: dict) -> dict:
    return {
        "source": row.get("source") or "tdxapi",
        "status": row.get("status"),
        "event_ts": _int_or_none(row.get("event_ts")),
        "ingest_ts": _int_or_none(row.get("ingest_ts")),
        "up_count": _num(row.get("up_count")),
        "down_count": _num(row.get("down_count")),
        "flat_count": _num(row.get("flat_count")),
        "total_count": _num(row.get("total_count")),
        "up_down_ratio": _num(row.get("up_down_ratio")),
        "market_temperature": row.get("market_temperature") or "unknown",
        "major_index_change_pct": _num(row.get("major_index_change_pct")),
        "major_indices": _json_load(row.get("major_indices"), []),
        "exchanges": _json_load(row.get("exchanges"), {}),
        "raw": _json_load(row.get("raw"), {}),
    }


def _snapshot_age_s(snapshot: dict) -> float:
    ingest_ts = _int_or_none(snapshot.get("ingest_ts")) or 0
    if ingest_ts <= 0:
        return float("inf")
    return max(0.0, time.time() - ingest_ts / 1000)


def _int_or_now(value) -> int:
    got = _int_or_none(value)
    return got if got is not None else int(datetime.now(UTC).timestamp() * 1000)


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_load(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
