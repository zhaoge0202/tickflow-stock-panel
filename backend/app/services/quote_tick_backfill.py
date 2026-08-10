"""动能回放 quote_ticks 补数据队列。"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.services import quote_tick_store

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
BACKFILL_CHUNK_SIZE = 80


class QuoteTickBackfillService:
    """把历史分钟线物化成 quote_ticks, 供动能气泡回放使用。"""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quote-tick-backfill")
        self._lock = threading.Lock()
        self._tasks: dict[tuple[str, date], dict[str, Any]] = {}

    def enqueue(
        self,
        data_dir: Path,
        target_date: date,
        *,
        repo: Any | None = None,
        reason: str = "missing_quote_ticks",
        force: bool = False,
        min_symbols: int | None = None,
    ) -> dict[str, Any]:
        key = (str(data_dir), target_date)
        required_symbols = max(0, int(min_symbols or 0))
        with self._lock:
            current = dict(self._tasks.get(key) or {})
            if current.get("status") in {"queued", "running"}:
                current["coalesced"] = True
                return current
            task = {
                "status": "queued",
                "date": target_date.isoformat(),
                "reason": reason,
                "queued_at": _now_iso(),
                "coalesced": False,
                "rows": 0,
                "symbols": 0,
                "min_symbols": required_symbols,
                "error": None,
            }
            self._tasks[key] = task

        self._executor.submit(
            self._run_task,
            key,
            Path(data_dir),
            target_date,
            repo,
            force,
            required_symbols,
        )
        return dict(task)

    def status(self, data_dir: Path, target_date: date) -> dict[str, Any]:
        key = (str(data_dir), target_date)
        with self._lock:
            current = dict(self._tasks.get(key) or {})
        if not current:
            return {"status": "idle", "date": target_date.isoformat()}
        return current

    def _set(self, key: tuple[str, date], patch: dict[str, Any]) -> None:
        with self._lock:
            current = dict(self._tasks.get(key) or {})
            current.update(patch)
            self._tasks[key] = current

    def _run_task(
        self,
        key: tuple[str, date],
        data_dir: Path,
        target_date: date,
        repo: Any | None,
        force: bool,
        min_symbols: int,
    ) -> None:
        started = time.time()
        self._set(key, {"status": "running", "started_at": _now_iso(), "error": None})
        try:
            local = quote_tick_store.materialize_from_minute(
                data_dir,
                target_date=target_date,
                force=force,
            )
            if local.get("status") in {"materialized", "exists"}:
                timeline = quote_tick_store.timeline_points(data_dir, target_date=target_date)
                if _timeline_meets_min_symbols(timeline, min_symbols):
                    self._set(key, {
                        "status": "succeeded",
                        "source": "local_minute",
                        "rows": local.get("rows") or 0,
                        "symbols": local.get("symbols") or timeline.get("symbol_count") or 0,
                        "hours": local.get("hours") or 0,
                        "points": timeline.get("count") or 0,
                        "min_symbols": min_symbols,
                        "finished_at": _now_iso(),
                        "elapsed_seconds": round(time.time() - started, 3),
                    })
                    return
                logger.info(
                    "local minute quote_ticks still sparse: date=%s symbols=%s min_symbols=%s",
                    target_date,
                    timeline.get("symbol_count") or 0,
                    min_symbols,
                )

            symbols = _resolve_stock_symbols(repo, data_dir)
            if not symbols:
                self._set(key, {
                    "status": "failed",
                    "error": "没有可用于补数据的股票列表",
                    "finished_at": _now_iso(),
                    "elapsed_seconds": round(time.time() - started, 3),
                })
                return

            minute_df = self._fetch_tdxapi_minute(symbols, target_date, key)
            if minute_df.is_empty():
                self._set(key, {
                    "status": "failed",
                    "source": "tdxapi_minute",
                    "symbols": len(symbols),
                    "rows": 0,
                    "error": "tdxapi 未返回该日分钟线",
                    "finished_at": _now_iso(),
                    "elapsed_seconds": round(time.time() - started, 3),
                })
                return

            _write_minute_partition(data_dir, target_date, minute_df)
            materialized = quote_tick_store.materialize_from_minute(
                data_dir,
                target_date=target_date,
                force=True,
            )
            timeline = quote_tick_store.timeline_points(data_dir, target_date=target_date)
            if _timeline_meets_min_symbols(timeline, min_symbols):
                status = "succeeded"
                error = None
            elif timeline.get("has_ticks"):
                status = "partial"
                error = "补数据完成但覆盖股票数不足"
            else:
                status = "failed"
                error = materialized.get("error") or "分钟线物化 quote_ticks 失败"
            self._set(key, {
                "status": status,
                "source": "tdxapi_minute",
                "rows": materialized.get("rows") or 0,
                "symbols": timeline.get("symbol_count") or materialized.get("symbols") or len(symbols),
                "min_symbols": min_symbols,
                "hours": materialized.get("hours") or 0,
                "points": timeline.get("count") or 0,
                "error": error,
                "finished_at": _now_iso(),
                "elapsed_seconds": round(time.time() - started, 3),
            })
        except Exception as exc:
            logger.warning("quote_ticks backfill failed(%s): %s", target_date, exc)
            self._set(key, {
                "status": "failed",
                "error": str(exc),
                "finished_at": _now_iso(),
                "elapsed_seconds": round(time.time() - started, 3),
            })

    def _fetch_tdxapi_minute(
        self,
        symbols: list[str],
        target_date: date,
        key: tuple[str, date],
    ) -> pl.DataFrame:
        from app.plugins.tdxapi.provider import TDXAPIProvider

        provider = TDXAPIProvider()
        frames: list[pl.DataFrame] = []
        start_dt = datetime.combine(target_date, dt_time(9, 30), tzinfo=CN_TZ)
        end_dt = datetime.combine(target_date, dt_time(15, 0), tzinfo=CN_TZ)
        try:
            chunks = list(_chunked(symbols, BACKFILL_CHUNK_SIZE))
            for index, chunk in enumerate(chunks, start=1):
                self._set(key, {
                    "progress": {
                        "current": index,
                        "total": len(chunks),
                        "symbols_done": min(index * BACKFILL_CHUNK_SIZE, len(symbols)),
                        "symbols_total": len(symbols),
                    }
                })
                try:
                    df = provider.get_minute(
                        chunk,
                        start_dt,
                        end_dt,
                        asset_type="stock",
                        freq="1m",
                    )
                except Exception as exc:
                    logger.warning("quote_ticks backfill minute chunk failed(%d/%d): %s", index, len(chunks), exc)
                    continue
                if df is not None and not df.is_empty():
                    frames.append(df)
        finally:
            provider.close()
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _resolve_stock_symbols(repo: Any | None, data_dir: Path) -> list[str]:
    if repo is not None:
        try:
            df = repo.get_instruments_asset("stock")
            symbols = _symbols_from_frame(df)
            if symbols:
                return symbols
        except Exception as exc:
            logger.debug("quote_ticks backfill repo instruments failed: %s", exc)

    path = data_dir / "instruments" / "instruments.parquet"
    if path.exists():
        try:
            symbols = _symbols_from_frame(pl.read_parquet(path, columns=["symbol"]))
            if symbols:
                return symbols
        except Exception as exc:
            logger.debug("quote_ticks backfill local instruments failed: %s", exc)

    try:
        from app.plugins.tdxapi.provider import TDXAPIProvider

        provider = TDXAPIProvider()
        try:
            rows = provider.get_instruments("stock") or []
        finally:
            provider.close()
        return sorted({
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        })
    except Exception as exc:
        logger.warning("quote_ticks backfill tdxapi instruments failed: %s", exc)
        return []


def _symbols_from_frame(df: pl.DataFrame | None) -> list[str]:
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return []
    return sorted({
        str(symbol or "").strip().upper()
        for symbol in df["symbol"].to_list()
        if str(symbol or "").strip()
    })


def _write_minute_partition(data_dir: Path, target_date: date, df: pl.DataFrame) -> None:
    if df.is_empty():
        return
    ds = target_date.isoformat()
    target_dir = data_dir / "kline_minute" / f"date={ds}"
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / "part.parquet"
    tmp_path = target_dir / f".part-{int(time.time() * 1000)}.parquet"
    df.write_parquet(tmp_path)
    tmp_path.replace(final_path)


def _chunked(items: list[str], size: int):
    size = max(1, int(size))
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _timeline_meets_min_symbols(timeline: dict[str, Any], min_symbols: int) -> bool:
    if not timeline.get("has_ticks"):
        return False
    required = max(0, int(min_symbols or 0))
    if required <= 0:
        return True
    return int(timeline.get("symbol_count") or 0) >= required


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


quote_tick_backfill_service = QuoteTickBackfillService()
