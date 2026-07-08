"""分钟 K API 刷新策略测试。"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import polars as pl

from app.api import kline


class _FrozenDate(dt.date):
    @classmethod
    def today(cls) -> dt.date:
        return cls(2026, 7, 8)


class _Repo:
    def __init__(self, local_df: pl.DataFrame):
        self.local_df = local_df

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def latest_minute_date(self, symbol: str, asset_type: str = "stock") -> dt.date | None:
        return dt.date(2026, 7, 8)

    def get_minute(self, symbol: str, trade_date: dt.date, asset_type: str = "stock") -> pl.DataFrame:
        return self.local_df


def _request(repo: _Repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _minute_df(day: dt.date, count: int, close: float) -> pl.DataFrame:
    start = dt.datetime(day.year, day.month, day.day, 9, 30)
    return pl.DataFrame({
        "symbol": ["000725.SZ"] * count,
        "datetime": [start + dt.timedelta(minutes=i) for i in range(count)],
        "open": [close] * count,
        "high": [close] * count,
        "low": [close] * count,
        "close": [close] * count,
        "volume": [100.0] * count,
        "amount": [close * 100.0] * count,
    })


def test_get_minute_today_prefers_live_over_complete_local(monkeypatch):
    monkeypatch.setattr(kline, "date", _FrozenDate)
    monkeypatch.setattr(kline, "_get_stock_info", lambda repo, symbol: {})

    local_df = _minute_df(dt.date(2026, 7, 8), 240, 7.30)
    live_df = _minute_df(dt.date(2026, 7, 8), 3, 7.40)
    calls: list[tuple[str, dt.date]] = []

    def fake_fetch(symbol: str, trade_date: dt.date) -> pl.DataFrame:
        calls.append((symbol, trade_date))
        return live_df

    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", fake_fetch)

    resp = kline.get_minute(_request(_Repo(local_df)), "000725.SZ", dt.date(2026, 7, 8))

    assert resp["source"] == "live"
    assert len(resp["rows"]) == 3
    assert resp["rows"][-1]["close"] == 7.40
    assert calls == [("000725.SZ", dt.date(2026, 7, 8))]


def test_get_minute_today_falls_back_to_local_when_live_empty(monkeypatch):
    monkeypatch.setattr(kline, "date", _FrozenDate)
    monkeypatch.setattr(kline, "_get_stock_info", lambda repo, symbol: {})

    local_df = _minute_df(dt.date(2026, 7, 8), 240, 7.30)
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", lambda symbol, trade_date: pl.DataFrame())

    resp = kline.get_minute(_request(_Repo(local_df)), "000725.SZ", dt.date(2026, 7, 8))

    assert resp["source"] == "local"
    assert len(resp["rows"]) == 240


def test_get_minute_history_uses_complete_local_without_live_fetch(monkeypatch):
    monkeypatch.setattr(kline, "date", _FrozenDate)
    monkeypatch.setattr(kline, "_get_stock_info", lambda repo, symbol: {})
    monkeypatch.setattr(
        kline.kline_sync,
        "fetch_minute_single",
        lambda symbol, trade_date: (_ for _ in ()).throw(AssertionError("历史完整分钟K不应拉 live")),
    )

    local_df = _minute_df(dt.date(2026, 7, 7), 240, 7.30)
    resp = kline.get_minute(_request(_Repo(local_df)), "000725.SZ", dt.date(2026, 7, 7))

    assert resp["source"] == "local"
    assert len(resp["rows"]) == 240
