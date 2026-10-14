"""Polars parquet helpers."""
from __future__ import annotations

import errno
import logging
import os
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl

logger = logging.getLogger(__name__)


def _lock_file_fd(stream: BinaryIO, timeout_s: float = 15.0) -> None:
    """带超时的非阻塞轮询跨进程锁。"""
    start = time.monotonic()
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            is_lock_blocked = (
                exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                or getattr(exc, "winerror", None) in {32, 33}
            )
            if not is_lock_blocked:
                raise
            if time.monotonic() - start >= timeout_s:
                raise TimeoutError(f"获取跨进程文件锁超时 ({timeout_s}s)") from exc
            time.sleep(0.05 + random.random() * 0.05)


def _unlock_file_fd(stream: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        logger.warning("释放文件锁失败", exc_info=True)


@contextmanager
def interprocess_file_lock(lock_path: Path, timeout_s: float = 15.0) -> Iterator[None]:
    """跨平台跨进程文件锁上下文管理器。

    使用操作系统底层文件锁 (Windows: msvcrt.locking, POSIX: fcntl.flock)。
    同一读改写事务的所有参与者必须使用同一锁文件。
    """
    if timeout_s < 0:
        raise ValueError("timeout_s must not be negative")
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
        except OSError:
            pass
        _lock_file_fd(stream, timeout_s=timeout_s)
        try:
            yield
        finally:
            _unlock_file_fd(stream)


def replace_with_retry(
    src: Path,
    dst: Path,
    *,
    attempts: int = 10,
    delay_s: float = 0.5,
) -> None:
    """原子替换 parquet, 并穿过 Windows 读端短暂占用。"""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if delay_s < 0:
        raise ValueError("delay_s must not be negative")

    last: Exception | None = None
    for index in range(attempts):
        try:
            src.replace(dst)
            if index:
                logger.info(
                    "parquet replace succeeded after %d blocked attempt(s): %s",
                    index,
                    dst,
                )
            return
        except OSError as exc:
            if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {32, 33}:
                raise
            last = exc
            if index == 0:
                logger.warning(
                    "parquet replace blocked by concurrent reader/writer, retrying "
                    "(total <= %.1fs): %s (%s)",
                    attempts * delay_s,
                    dst,
                    exc,
                )
            if index < attempts - 1:
                # 增加微抖动, 避免多个重试进程同频共振
                jitter = delay_s * (0.9 + 0.2 * random.random())
                time.sleep(jitter)

    raise last  # type: ignore[misc]  # attempts >= 1 时 last 必已赋值


def _write_parquet_snapshot(
    df: pl.DataFrame, out: Path, *, attempts: int = 10, delay_s: float = 0.5,
) -> None:
    """调用方持有目标文件锁; 独占临时文件只承载本次快照。"""
    tmp = out.with_name(f".{out.name}.{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex}.tmp")
    try:
        df.write_parquet(tmp)
        replace_with_retry(tmp, out, attempts=attempts, delay_s=delay_s)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_parquet(
    df: pl.DataFrame,
    out: Path,
    *,
    lock_timeout_s: float = 15.0,
    attempts: int = 10,
    delay_s: float = 0.5,
) -> None:
    """原子覆盖完整快照; 增量合并应使用 atomic_update_parquet。"""
    out = Path(out)
    with interprocess_file_lock(out.with_name(f".{out.name}.lock"), lock_timeout_s):
        _write_parquet_snapshot(df, out, attempts=attempts, delay_s=delay_s)


def atomic_update_parquet(
    out: Path,
    update: Callable[[pl.DataFrame], pl.DataFrame],
    *,
    lock_timeout_s: float = 15.0,
) -> tuple[int, int]:
    """在单文件跨进程锁内读、合并和写入, 返回合并前后行数。

    update 只能进行当前分区的本地计算, 不执行网络请求或全量历史扫描。
    失败时保留旧文件; 与完整快照写入共用同一锁, 避免读取旧基底后丢更新。
    """
    out = Path(out)
    with interprocess_file_lock(out.with_name(f".{out.name}.lock"), lock_timeout_s):
        existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()
        merged = update(existing)
        _write_parquet_snapshot(merged, out)
        return existing.height, merged.height


DAILY_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "quote_ts": pl.Int64,
}

ENRICHED_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "auction_result_price": pl.Float64,
    "auction_result_volume": pl.Float64,
    "auction_result_amount": pl.Float64,
    "auction_tick_count": pl.Float64,
    "auction_tick_seconds": pl.Float64,
    "auction_trade_price_change": pl.Float64,
    "auction_trade_price_range": pl.Float64,
    "auction_trade_volume_delta": pl.Float64,
    "auction_trade_volume_per_second": pl.Float64,
    "auction_trade_unmatched_ratio": pl.Float64,
    "auction_trade_pressure_score": pl.Float64,
    "auction_trade_depth_imbalance": pl.Float64,
    "auction_trade_spread_pct": pl.Float64,
    "raw_close": pl.Float64,
    "raw_high": pl.Float64,
    "raw_low": pl.Float64,
    "turnover_rate": pl.Float64,
    "consecutive_limit_ups": pl.UInt32,
    "consecutive_limit_downs": pl.UInt32,
    "quote_ts": pl.Int64,
}


def scan_parquet_compat(source: Any, **kwargs: Any) -> pl.LazyFrame:
    """Scan partitioned parquet while tolerating additive schema changes."""
    kwargs.setdefault("missing_columns", "insert")
    kwargs.setdefault("extra_columns", "ignore")
    return pl.scan_parquet(source, **kwargs)


def scan_daily_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", DAILY_STORAGE_SCHEMA)
    kwargs.setdefault("cast_options", pl.ScanCastOptions(integer_cast="allow-float"))
    return scan_parquet_compat(source, **kwargs)


def scan_enriched_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", ENRICHED_STORAGE_SCHEMA)
    kwargs.setdefault("cast_options", pl.ScanCastOptions(integer_cast="allow-float"))
    return scan_parquet_compat(source, **kwargs)
