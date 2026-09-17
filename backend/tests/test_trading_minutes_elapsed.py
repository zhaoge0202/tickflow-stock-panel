"""当日已交易分钟数 — 量比时间折算的分母来源。

compute_enriched_today 用 `time_factor = 240 / elapsed_minutes` 把盘中的部分
成交量折算到全天量级。周末/非交易日没有"当日已交易分钟", 必须按全天 240 算,
否则手动刷新一次行情就会把全市场量比放大 240/已过分钟数 倍, 「放量」
(vol_ratio_5d >= 2) 整片误触发。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market_time import (
    trading_minutes_elapsed_from_dt,
    trading_minutes_elapsed_from_ts,
)

CN = timezone(timedelta(hours=8))


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.mark.parametrize(
    ("moment", "label"),
    [
        (datetime(2026, 9, 12, 9, 31, tzinfo=CN), "周六 开盘后 1 分钟"),
        (datetime(2026, 9, 12, 10, 0, tzinfo=CN), "周六 上午"),
        (datetime(2026, 9, 13, 14, 0, tzinfo=CN), "周日 下午"),
    ],
)
def test_weekend_counts_as_a_full_session(moment, label):
    assert trading_minutes_elapsed_from_dt(moment) == 240.0, label


def test_weekend_timestamp_also_counts_as_full_session():
    """行情戳落在周末 (源侧异常戳) 时同样按全天算。"""
    assert trading_minutes_elapsed_from_ts(_ms(datetime(2026, 9, 12, 10, 0, tzinfo=CN))) == 240.0


@pytest.mark.parametrize(
    ("moment", "expected", "label"),
    [
        (datetime(2026, 9, 11, 9, 0, tzinfo=CN), 0.0, "周五 开盘前"),
        (datetime(2026, 9, 11, 9, 31, tzinfo=CN), 1.0, "周五 9:31"),
        (datetime(2026, 9, 11, 11, 30, tzinfo=CN), 120.0, "周五 午休起点"),
        (datetime(2026, 9, 11, 12, 30, tzinfo=CN), 120.0, "周五 午休中"),
        (datetime(2026, 9, 11, 14, 0, tzinfo=CN), 180.0, "周五 14:00"),
        (datetime(2026, 9, 11, 15, 30, tzinfo=CN), 240.0, "周五 收盘后"),
    ],
)
def test_weekday_session_progress_unchanged(moment, expected, label):
    """交易日的分段折算不受影响 (回归保护)。"""
    assert trading_minutes_elapsed_from_dt(moment) == expected, label


def test_missing_timestamp_still_counts_as_full_session():
    assert trading_minutes_elapsed_from_ts(None) == 240.0
    assert trading_minutes_elapsed_from_ts(0) == 240.0
