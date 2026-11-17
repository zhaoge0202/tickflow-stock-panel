"""正式日线策略的数据日期口径。"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

from app.market_time import (
    DAILY_STRATEGY_READY_TIME,
    INTRADAY_PREVIEW_READY_TIME,
    cn_now,
    latest_completed_strategy_date,
)
from app.tickflow.repository import enriched_dirname

# 只缓存慢变的分区日期输入; 完成日期仍逐请求按北京时间计算, 不缓存15:30门控结果。
# 根目录变化立即失效; 已存在的空分区补入文件最迟在短 TTL 后可见。
_DATE_CACHE_TTL_S = 30.0
_DATE_CACHE_MAX_ENTRIES = 32
_date_cache: OrderedDict[
    tuple[str, str], tuple[float, tuple[int, ...], tuple[date, ...]]
] = OrderedDict()
_date_cache_lock = threading.Lock()


def enriched_partition_dates(data_dir: Path, asset_type: str = "stock") -> list[date]:
    """返回存在完整 parquet 文件的 enriched 分区日期。"""
    root = Path(data_dir) / enriched_dirname(asset_type)
    if not root.exists():
        return []

    dates: list[date] = []
    for partition in root.glob("date=*"):
        if not partition.is_dir() or not (partition / "part.parquet").is_file():
            continue
        try:
            dates.append(date.fromisoformat(partition.name.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(set(dates))


def latest_strategy_date(
    data_dir: Path,
    asset_type: str = "stock",
    *,
    now: datetime | None = None,
) -> date | None:
    """返回当前允许正式日线策略使用的最新 enriched 日期。"""
    # 显式 now 用于测试/回放: 读取当前分区, 保留调用方的精确输入语义。
    if now is not None:
        return latest_completed_strategy_date(
            enriched_partition_dates(data_dir, asset_type),
            now=now,
        )
    current = cn_now()
    root = Path(data_dir) / enriched_dirname(asset_type)
    key = (str(root.resolve()), asset_type)
    started = time.monotonic()
    signature = _partition_root_signature(root)
    if signature is None:
        with _date_cache_lock:
            _date_cache.pop(key, None)
        return None
    with _date_cache_lock:
        hit = _date_cache.get(key)
        if hit is not None:
            _date_cache.move_to_end(key)
    if hit is not None and started - hit[0] < _DATE_CACHE_TTL_S and hit[1] == signature:
        value = latest_completed_strategy_date(hit[2], now=current)
        # 清数据可能只删分区内的文件, 不改根目录时间。只核对将返回的一天,
        # 避免把已删除分区当成正式日期, 且不用每轮 stat 数千个历史文件。
        if value is None or (root / f"date={value}" / "part.parquet").is_file():
            return value
    dates = tuple(enriched_partition_dates(data_dir, asset_type))
    value = latest_completed_strategy_date(dates, now=current)
    if _partition_root_signature(root) == signature:
        with _date_cache_lock:
            _date_cache[key] = (time.monotonic(), signature, dates)
            _date_cache.move_to_end(key)
            while len(_date_cache) > _DATE_CACHE_MAX_ENTRIES:
                _date_cache.popitem(last=False)
    return value


def _partition_root_signature(root: Path) -> tuple[int, ...] | None:
    try:
        stat = root.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns


def reject_intraday_strategy_date(
    requested: date,
    *,
    now: datetime | None = None,
    allow_preview: bool = False,
) -> None:
    """拒绝盘中显式请求当天日线策略，防止 API 绕过前端日期门控。

    若 allow_preview=True 且当前时间达到 14:50，则允许用于尾盘初选草稿版。
    """
    now = now or cn_now()
    if requested == now.date() and now.weekday() < 5:
        if allow_preview and now.time() >= INTRADAY_PREVIEW_READY_TIME:
            return
        if now.time() < DAILY_STRATEGY_READY_TIME:
            raise ValueError(
                f"盘中 {now.date()} 尚未收盘，正式日线策略请使用最近已完成交易日"
            )


def cache_generated_after_cutoff(
    as_of: date,
    updated_at_ms: object,
    *,
    now: datetime | None = None,
) -> bool:
    """判断当天策略缓存是否至少在 15:30 后生成。"""
    now = now or cn_now()
    if as_of != now.date() or now.weekday() >= 5:
        return True
    cutoff = datetime.combine(now.date(), DAILY_STRATEGY_READY_TIME, tzinfo=now.tzinfo)
    try:
        return float(updated_at_ms) >= cutoff.timestamp() * 1000
    except (TypeError, ValueError):
        return False
