"""资产类别交易规则档案测试 (CONTRIBUTING §3.3 日期归属 / §5.3 回测口径)。

覆盖:
  - A 股规则逐字段保持既有口径 (T+1 / 100 股 / 252 / 上证基准), 防止接入
    非 A 股资产时顺手改动 A 股行为;
  - 永续合约规则 (T+0 / 无涨跌停 / 无复权 / 小数仓位 / 365 / 无基准);
  - 未知资产类别 fail-closed;
  - 日归属: 两类资产同为北京自然日, 日边界 = 北京 00:00 (口径由交易所日线切日决定,
    见 market_rules 的选源约束 —— OKX 官方日线就是 UTC+8 零点);
  - 带时区入参归一、已过分钟数。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.market_rules import (
    CN_ASSET_TYPES,
    CRYPTO,
    contract_day_end,
    contract_day_start,
    contract_minutes_per_day,
    contract_today,
    elapsed_minutes,
    get_rules,
    is_crypto,
    is_trading_day_for,
    is_trading_session,
    to_contract_date,
)


def test_cn_rules_keep_existing_a_share_semantics():
    for asset_type in CN_ASSET_TYPES:
        rules = get_rules(asset_type)
        assert rules.t_plus == 1
        assert rules.price_limit is True
        assert rules.adjust is True
        assert rules.lot_size == 100
        assert rules.share_capital is True
        assert rules.halt_filter is True
        assert rules.session_24x7 is False
        assert rules.calendar == "cn"
        assert rules.annual_days == 252
        assert rules.default_benchmark == "000001.SH"


def test_empty_asset_type_is_treated_as_stock():
    # 历史调用方可能传 None / 空串, 语义按股票 (不得变成未知类别抛错)
    assert get_rules(None).asset_type == "stock"
    assert get_rules("").asset_type == "stock"
    assert get_rules("  STOCK  ").asset_type == "stock"


def test_unknown_asset_type_fails_closed():
    with pytest.raises(ValueError):
        get_rules("futures_comex")
    with pytest.raises(ValueError):
        get_rules("crypto_spot")


def test_crypto_rules_are_not_a_share_semantics():
    rules = get_rules(CRYPTO)
    assert rules.t_plus == 0
    assert rules.price_limit is False
    assert rules.adjust is False
    assert rules.lot_size is None
    assert rules.share_capital is False
    assert rules.halt_filter is False
    assert rules.session_24x7 is True
    assert rules.calendar == "none"
    assert rules.annual_days == 365
    # 无合适基准: 不得默默套用上证指数
    assert rules.default_benchmark is None


def test_is_crypto_only_matches_crypto():
    assert is_crypto(CRYPTO) is True
    assert is_crypto("CRYPTO") is True
    assert is_crypto("stock") is False
    assert is_crypto(None) is False


@pytest.mark.parametrize(
    ("beijing_wallclock", "expected"),
    [
        # 口径 = 北京自然日 (与 OKX 官方日线切日一致): 北京 00:00 是切换点
        (datetime(2026, 10, 13, 0, 0, 0), date(2026, 10, 13)),
        (datetime(2026, 10, 13, 7, 59, 59), date(2026, 10, 13)),
        (datetime(2026, 10, 13, 8, 0, 0), date(2026, 10, 13)),
        (datetime(2026, 10, 13, 23, 59, 59), date(2026, 10, 13)),
        (datetime(2026, 10, 14, 0, 0, 0), date(2026, 10, 14)),
    ],
)
def test_crypto_contract_date_uses_beijing_day(beijing_wallclock, expected):
    assert to_contract_date(beijing_wallclock, CRYPTO) == expected


@pytest.mark.parametrize(
    ("beijing_wallclock", "expected"),
    [
        (datetime(2026, 10, 13, 0, 0, 0), date(2026, 10, 13)),
        (datetime(2026, 10, 13, 23, 59, 59), date(2026, 10, 13)),
        (datetime(2026, 10, 14, 0, 0, 0), date(2026, 10, 14)),
    ],
)
def test_stock_contract_date_is_cn_day(beijing_wallclock, expected):
    assert to_contract_date(beijing_wallclock, "stock") == expected


def test_contract_date_normalizes_tz_aware_input():
    # 项目契约是北京墙钟 naive; 带时区入参先换算到北京, 不得按 UTC 直接取日
    utc_dt = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)  # 北京 09:00
    assert to_contract_date(utc_dt, CRYPTO) == date(2026, 10, 13)
    assert to_contract_date(utc_dt, "stock") == date(2026, 10, 13)


def test_contract_today_accepts_explicit_now():
    assert contract_today(CRYPTO, datetime(2026, 10, 13, 6, 0)) == date(2026, 10, 13)
    assert contract_today("stock", datetime(2026, 10, 13, 6, 0)) == date(2026, 10, 13)


def test_contract_day_bounds():
    assert contract_day_start(date(2026, 10, 13), CRYPTO) == datetime(2026, 10, 13, 0, 0)
    assert contract_day_end(date(2026, 10, 13), CRYPTO) == datetime(2026, 10, 14, 0, 0)
    assert contract_day_start(date(2026, 10, 13), "stock") == datetime(2026, 10, 13, 0, 0)
    assert contract_day_end(date(2026, 10, 13), "stock") == datetime(2026, 10, 14, 0, 0)


def test_contract_day_bounds_cover_the_whole_day_without_gap():
    # 相邻合约日首尾相接: 上一日终点 == 下一日起点 (分区不重叠不丢分钟)
    for asset_type in (CRYPTO, "stock"):
        assert contract_day_end(date(2026, 10, 13), asset_type) == contract_day_start(
            date(2026, 10, 14), asset_type
        )


def test_minutes_per_contract_day():
    assert contract_minutes_per_day("stock") == 240
    assert contract_minutes_per_day(CRYPTO) == 1440


def test_crypto_trading_session_is_always_open():
    # 周六凌晨与周日深夜都必须是交易时段 (24x7)
    assert is_trading_session(datetime(2026, 10, 17, 3, 0), CRYPTO) is True
    assert is_trading_session(datetime(2026, 10, 18, 23, 30), CRYPTO) is True
    # A 股同一时刻不是交易时段
    assert is_trading_session(datetime(2026, 10, 17, 3, 0), "stock") is False
    assert is_trading_session(datetime(2026, 10, 13, 10, 30), "stock") is True


def test_crypto_trading_day_is_always_true():
    assert is_trading_day_for(CRYPTO, datetime(2026, 10, 17, 3, 0)) is True
    assert is_trading_day_for(CRYPTO, datetime(2026, 10, 18, 23, 30)) is True


def test_elapsed_minutes_crypto_covers_full_contract_day():
    # 2026-10-13 从北京 00:00 起算, 24 小时铺满
    assert elapsed_minutes(datetime(2026, 10, 13, 0, 0), CRYPTO) == 0.0
    assert elapsed_minutes(datetime(2026, 10, 13, 20, 0), CRYPTO) == 1200.0
    assert elapsed_minutes(datetime(2026, 10, 13, 7, 0), CRYPTO) == 420.0
    # 上界收敛到 1440, 不越界
    assert elapsed_minutes(datetime(2026, 10, 14, 0, 0), CRYPTO) == 0.0
    assert elapsed_minutes(datetime(2026, 10, 13, 23, 59), CRYPTO) == pytest.approx(1439.0)


def test_elapsed_minutes_stock_keeps_two_session_folding():
    # A 股: 10:30 = 60 分钟; 午休保持 120; 收盘后 240
    assert elapsed_minutes(datetime(2026, 10, 13, 10, 30), "stock") == 60.0
    assert elapsed_minutes(datetime(2026, 10, 13, 12, 0), "stock") == 120.0
    assert elapsed_minutes(datetime(2026, 10, 13, 16, 0), "stock") == 240.0
