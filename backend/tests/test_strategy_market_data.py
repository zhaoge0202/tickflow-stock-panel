"""策略指数K线访问模块 — 测试。"""
import datetime

import polars as pl
import pytest

from app.strategy import market_data
from app.strategy.ai_generator import AIStrategyGenerator


def test_whitelist_allows_market_data_import():
    AIStrategyGenerator._validate_safety(
        "from app.strategy.market_data import get_index_daily, get_daily"
    )


def test_whitelist_still_blocks_dangerous():
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("import os")
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("from os import path")
    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety("getattr(obj, '__globals__')")


class _FakeRepo:
    """最小 fake: 只实现 market_data 用到的接口。"""
    def __init__(self, index_df=None):
        self.calls: list[tuple] = []
        self._asset = {"000001.SH": "index", "510300.SH": "etf", "600000.SH": "stock"}
        self._index_df = index_df if index_df is not None else pl.DataFrame(
            {"date": ["2026-01-02"], "close": [3000.0], "macd_dif": [1.0], "macd_dea": [2.0]}
        )
        self._empty = pl.DataFrame()

    def resolve_asset_type(self, symbol):
        self.calls.append(("resolve", symbol))
        return self._asset.get(symbol, "stock")

    def get_index_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("index", symbol, start, end, columns))
        return self._index_df if symbol == "000001.SH" else self._empty

    def get_etf_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("etf", symbol, start, end, columns))
        return self._empty

    def get_daily(self, symbol, start=None, end=None, columns=None):
        self.calls.append(("stock", symbol, start, end, columns))
        return self._empty

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})


@pytest.fixture()
def fake_repo():
    fake = _FakeRepo()
    market_data._set_repo(fake)
    yield fake
    market_data._reset_repo()


def test_get_index_daily_delegates_and_normalizes_dates(fake_repo):
    df = market_data.get_index_daily(
        "000001.SH", start="2026-01-01", end="2026-01-31", columns=["date", "close"]
    )
    assert df.height == 1 and df["close"][0] == 3000.0
    _, sym, s, e, cols = fake_repo.calls[-1]
    assert sym == "000001.SH"
    assert s == datetime.date(2026, 1, 1)
    assert e == datetime.date(2026, 1, 31)
    assert cols == ["date", "close"]


@pytest.mark.parametrize("symbol,expected_kind", [
    ("000001.SH", "index"),
    ("510300.SH", "etf"),
    ("600000.SH", "stock"),
])
def test_get_daily_dispatch_by_asset_type(fake_repo, symbol, expected_kind):
    market_data.get_daily(symbol)
    last = fake_repo.calls[-1]
    assert last[0] == expected_kind
    assert last[1] == symbol


def test_bad_symbol_returns_empty_without_calling_repo(fake_repo):
    assert market_data.get_index_daily("").is_empty()
    assert market_data.get_index_daily(None).is_empty()
    assert market_data.get_etf_daily("").is_empty()
    assert market_data.get_daily(None).is_empty()
    assert fake_repo.calls == []


def test_missing_symbol_returns_empty_no_raise(fake_repo):
    assert market_data.get_index_daily("999999.SH").is_empty()


def test_list_index_symbols(fake_repo):
    assert market_data.list_index_symbols() == [{"symbol": "000001.SH", "name": "上证指数"}]
