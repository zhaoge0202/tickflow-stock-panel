"""Polars parquet helpers."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)


def replace_with_retry(
    src: Path,
    dst: Path,
    *,
    attempts: int = 10,
    delay_s: float = 0.5,
) -> None:
    """原子替换 parquet，并穿过 Windows 读端的短暂文件占用。"""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if delay_s < 0:
        raise ValueError("delay_s must not be negative")

    last: PermissionError | None = None
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
        except PermissionError as exc:
            last = exc
            if index == 0:
                logger.warning(
                    "parquet replace blocked by concurrent reader, retrying "
                    "(total <= %.1fs): %s",
                    attempts * delay_s,
                    dst,
                )
            if index < attempts - 1:
                time.sleep(delay_s)

    raise last  # type: ignore[misc]  # attempts >= 1 时 last 必已赋值

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
