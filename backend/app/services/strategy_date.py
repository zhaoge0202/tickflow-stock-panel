"""正式日线策略的数据日期口径。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.market_time import (
    DAILY_STRATEGY_READY_TIME,
    cn_now,
    latest_completed_strategy_date,
)
from app.tickflow.repository import enriched_dirname


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
    return latest_completed_strategy_date(
        enriched_partition_dates(data_dir, asset_type),
        now=now,
    )


def reject_intraday_strategy_date(
    requested: date,
    *,
    now: datetime | None = None,
) -> None:
    """拒绝盘中显式请求当天日线策略，防止 API 绕过前端日期门控。"""
    now = now or cn_now()
    if (
        requested == now.date()
        and now.weekday() < 5
        and now.time() < DAILY_STRATEGY_READY_TIME
    ):
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
