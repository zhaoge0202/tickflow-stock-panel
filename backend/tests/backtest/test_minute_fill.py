"""分钟K精确成交 (_resolve_minute_fill / _load_minute_for_fills) 回归测试。

背景: 原实现用 df.to_numpy() 转 structured array 再按字段名索引 (arr["open"])。
当 DataFrame 含 datetime 列 + float 列时, to_numpy() 退化为 dtype=object 的二维
数组, 字段名索引抛 IndexError: "only integers, slices... are valid indices"。
开启 minute_fill 的回测从未成功跑通过。此测试锁定该 bug 不再复发。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.backtest.engine import BacktestEngine


def _sample_minute_df(symbol: str = "000001.SZ") -> pl.DataFrame:
    """构造一份带 datetime 列 + float 列的分钟K (复现 to_numpy 退化的场景)。"""
    base = datetime(2024, 1, 2, 9, 31)
    return pl.DataFrame({
        "symbol": [symbol] * 4,
        "datetime": [base.replace(hour=h, minute=m) for h, m in
                     [(9, 31), (10, 0), (14, 0), (14, 57)]],
        "open": [10.0, 10.5, 10.8, 10.6],
        "high": [10.6, 10.7, 10.9, 10.7],
        "low": [9.9, 10.4, 10.7, 10.5],
        "close": [10.2, 10.6, 10.85, 10.65],
        "volume": [100, 200, 150, 120],
        "amount": [1020.0, 2120.0, 1627.0, 1278.0],
    })


def test_resolve_minute_fill_with_mixed_columns_no_index_error():
    """混合列类型 (datetime + float) 不再抛 IndexError。

    这是原 bug 的精确复现点: 旧实现 _resolve_minute_fill 接收 ndarray,
    arr["open"] 在 object 数组上会炸。现在接受 DataFrame, 按列取值。
    """
    mdf = _sample_minute_df()
    # 三种分支都应正常返回, 不抛 IndexError
    assert BacktestEngine._resolve_minute_fill(mdf, ref_price=10.5, side="buy") is not None
    assert BacktestEngine._resolve_minute_fill(mdf, ref_price=10.5, side="sell") is not None
    # 无参考线 → VWAP 分支
    vwap = BacktestEngine._resolve_minute_fill(mdf, ref_price=None, side="buy")
    assert vwap is not None and vwap > 0


def test_resolve_minute_fill_buy_cross_above_ref():
    """买入: 价格涨破参考线 → 开盘已高于则按开盘。"""
    mdf = _sample_minute_df()
    # ref=9.5, 开盘 10.0 已高于 → 按开盘
    assert BacktestEngine._resolve_minute_fill(mdf, 9.5, "buy") == 10.0


def test_resolve_minute_fill_sell_cross_below_ref():
    """卖出: 价格跌破参考线 → 开盘已低于则按开盘。"""
    mdf = _sample_minute_df()
    # ref=10.5, 开盘 10.0 已低于 → 按开盘
    assert BacktestEngine._resolve_minute_fill(mdf, 10.5, "sell") == 10.0


def test_resolve_minute_fill_vwap():
    """无参考线 → VWAP = 总成交额 / 总成交量。"""
    mdf = _sample_minute_df()
    total_amt = 1020.0 + 2120.0 + 1627.0 + 1278.0
    total_vol = 100 + 200 + 150 + 120
    expected = total_amt / total_vol
    assert BacktestEngine._resolve_minute_fill(mdf, None, "buy") == expected


def test_resolve_minute_fill_empty_returns_none():
    """空 DataFrame → None (降级到日K口径)。"""
    assert BacktestEngine._resolve_minute_fill(pl.DataFrame(), None, "buy") is None


class _FakeRepo:
    """最小 repo 桩: get_minute_range 直接返回预构造的混合列 DataFrame。"""

    def __init__(self, df: pl.DataFrame) -> None:
        self._df = df

    def get_minute_range(self, symbols, start, end, asset_type="stock") -> pl.DataFrame:  # noqa: ANN001
        return self._df


def test_load_minute_for_fills_returns_dataframe_dict():
    """_load_minute_for_fills 返回 {(symbol, date_str): DataFrame}, 而非 ndarray。

    锁定: cache 值类型必须是 pl.DataFrame (旧实现返回的对象后续被 .to_numpy()
    退化成 object 数组触发 bug)。
    """
    df = _sample_minute_df()
    repo = _FakeRepo(df)
    result = BacktestEngine._load_minute_for_fills(
        repo, ["000001.SZ"], {"2024-01-02"}, "stock",
    )
    assert ("000001.SZ", "2024-01-02") in result
    val = result[("000001.SZ", "2024-01-02")]
    # 关键断言: 返回的是 DataFrame, 可直接喂给 _resolve_minute_fill
    assert isinstance(val, pl.DataFrame)
    assert not val.is_empty()
    # 端到端: load → resolve 不抛异常
    price = BacktestEngine._resolve_minute_fill(val, None, "buy")
    assert price is not None and price > 0
