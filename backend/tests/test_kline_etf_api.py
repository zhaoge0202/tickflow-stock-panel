from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import kline


class _Repo:
    def __init__(self, *, etf_local: bool = True) -> None:
        self.stock_batch_symbols: list[str] = []
        self.etf_daily_symbols: list[str] = []
        self.etf_local = etf_local
        self._frames = {
            "stock": pl.DataFrame([{"symbol": "002491.SZ", "code": "002491", "name": "通鼎互联"}]),
            "etf": pl.DataFrame([{"symbol": "589020.SH", "code": "589020", "name": "科创半导体设备ETF鹏华"}]),
            "index": pl.DataFrame(),
        }

    def get_instruments_asset(self, asset_type: str):
        return self._frames.get(asset_type, pl.DataFrame())

    def get_etf_symbol_set(self) -> set[str]:
        return {"589020.SH"}

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.stock_batch_symbols = list(symbols)
        return _daily_df("002491.SZ", 7.4).select(columns)

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        assert asset_type == "etf"
        self.etf_daily_symbols.append(symbol)
        if not self.etf_local:
            return pl.DataFrame()
        return _daily_df(symbol, 3.08).select(columns)


def _daily_df(symbol: str, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "date": [date(2026, 7, 8)],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [100.0],
    })


def _request(repo: _Repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_daily_batch_splits_normalized_etf_symbols() -> None:
    repo = _Repo()

    resp = kline.get_daily_batch(_request(repo), {"symbols": ["002491", "589020"], "days": 5})

    assert repo.stock_batch_symbols == ["002491.SZ"]
    assert repo.etf_daily_symbols == ["589020.SH"]
    assert set(resp["data"]) == {"002491.SZ", "589020.SH"}
    assert resp["data"]["589020.SH"][0]["close"] == 3.08


def test_daily_batch_fetches_missing_etf_live(monkeypatch) -> None:
    repo = _Repo(etf_local=False)
    calls: list[tuple[list[str], int]] = []

    def fake_sync_daily_batch(symbols, count):
        calls.append((list(symbols), count))
        return _daily_df("589020.SH", 3.09)

    monkeypatch.setattr(kline.kline_sync, "sync_daily_batch", fake_sync_daily_batch)

    resp = kline.get_daily_batch(_request(repo), {"symbols": ["589020"], "days": 5})

    assert calls == [(["589020.SH"], 35)]
    assert set(resp["data"]) == {"589020.SH"}
    assert resp["data"]["589020.SH"][0]["close"] == 3.09
