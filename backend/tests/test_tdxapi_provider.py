"""TDXAPIProvider 归一化与插件注册测试。"""
from __future__ import annotations

import datetime as dt

import polars as pl

from app.plugins.tdxapi import provider as tp
from app.plugins.tdxapi.provider import TDXAPIProvider


def _patch_request(monkeypatch, mapping):
    def fake(self, method, path, **kwargs):
        value = mapping[(method, path)]
        return value(kwargs) if callable(value) else value

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)


def test_get_daily_normalizes_tdx_kline(monkeypatch):
    _patch_request(monkeypatch, {
        ("GET", "/api/kline-all/tdx"): {
            "list": [
                {
                    "Time": "2026-01-05T00:00:00+08:00",
                    "Open": 12300,
                    "High": 12600,
                    "Low": 12280,
                    "Close": 12500,
                    "Volume": 123,
                    "Amount": 156000000,
                },
                {
                    "Time": "2025-12-31T00:00:00+08:00",
                    "Open": 10000,
                    "High": 10000,
                    "Low": 10000,
                    "Close": 10000,
                    "Volume": 1,
                    "Amount": 1,
                },
            ],
        },
    })

    df = TDXAPIProvider().get_daily(
        ["002491.SZ"],
        dt.datetime(2026, 1, 1),
        dt.datetime(2026, 1, 31),
    )

    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.height == 1
    assert df["symbol"][0] == "002491.SZ"
    assert df["date"][0] == dt.date(2026, 1, 5)
    assert df["open"][0] == 12.3
    assert df["volume"][0] == 123
    assert df.schema["date"] == pl.Date


def test_get_minute_normalizes_tdx_minute_kline(monkeypatch):
    _patch_request(monkeypatch, {
        ("GET", "/api/kline-all/tdx"): {
            "list": [
                {
                    "Time": "2026-01-05T09:31:00+08:00",
                    "Open": 12300,
                    "High": 12600,
                    "Low": 12280,
                    "Close": 12500,
                    "Volume": 2,
                    "Amount": 300000,
                },
            ],
        },
    })

    df = TDXAPIProvider().get_minute(
        ["002491.SZ"],
        dt.datetime(2026, 1, 5, 9, 30),
        dt.datetime(2026, 1, 5, 15, 0),
    )

    assert set(df.columns) == {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
    assert df.height == 1
    assert df["datetime"][0] == dt.datetime(2026, 1, 5, 9, 31)
    assert df["close"][0] == 12.5
    assert df["volume"][0] == 2


def test_get_realtime_batches_and_maps_quote(monkeypatch):
    def fake_codes(kwargs):
        return {"codes": [{"code": "002491", "name": "通鼎互联", "exchange": "sz"}]}

    def fake_quote(kwargs):
        assert kwargs["json"]["codes"] == ["sz002491"]
        return [
            {
                "Exchange": 0,
                "Code": "002491",
                "K": {"Last": 12000, "Open": 12100, "High": 12600, "Low": 11900, "Close": 12500},
                "TotalHand": 10,
                "Amount": 1000000,
                "ServerTime": "1730617200",
            },
        ]

    _patch_request(monkeypatch, {
        ("GET", "/api/codes"): fake_codes,
        ("POST", "/api/batch-quote"): fake_quote,
    })

    rows = TDXAPIProvider().get_realtime(symbols=["002491.SZ"])

    assert len(rows) == 1
    assert rows[0]["symbol"] == "002491.SZ"
    assert rows[0]["name"] == "通鼎互联"
    assert rows[0]["last_price"] == 12.5
    assert rows[0]["prev_close"] == 12.0
    assert rows[0]["volume"] == 10
    assert abs(rows[0]["change_pct"] - (0.5 / 12.0)) < 1e-12


def test_get_realtime_with_symbols_survives_codes_failure(monkeypatch):
    def fake(self, method, path, **kwargs):
        if path == "/api/codes":
            raise RuntimeError("codes down")
        assert kwargs["json"]["codes"] == ["sz002491"]
        return [
            {
                "Exchange": 0,
                "Code": "002491",
                "K": {"Last": 12000, "Open": 12100, "High": 12600, "Low": 11900, "Close": 12500},
                "TotalHand": 10,
                "Amount": 1000000,
                "ServerTime": "1730617200",
            },
        ]

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)

    rows = TDXAPIProvider().get_realtime(symbols=["002491.SZ"])

    assert len(rows) == 1
    assert rows[0]["symbol"] == "002491.SZ"
    assert rows[0]["name"] is None


def test_get_daily_continues_when_one_symbol_fails(monkeypatch):
    def fake(self, method, path, **kwargs):
        code = kwargs["params"]["code"]
        if code == "sz000001":
            raise RuntimeError("tdx timeout")
        return {
            "list": [
                {
                    "Time": "2026-01-05T00:00:00+08:00",
                    "Open": 12300,
                    "High": 12600,
                    "Low": 12280,
                    "Close": 12500,
                    "Volume": 2,
                    "Amount": 300000,
                },
            ],
        }

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)

    df = TDXAPIProvider().get_daily(["000001.SZ", "sz002491"], None, None)

    assert df.height == 1
    assert df["symbol"][0] == "002491.SZ"


def test_get_instruments_from_codes(monkeypatch):
    _patch_request(monkeypatch, {
        ("GET", "/api/codes"): {
            "codes": [
                {"code": "600519", "name": "贵州茅台", "exchange": "sh"},
                {"code": "002491", "name": "通鼎互联", "exchange": "sz"},
            ],
        },
    })

    rows = TDXAPIProvider().get_instruments("stock")
    assert rows[0]["symbol"] == "600519.SH"
    assert rows[1]["symbol"] == "002491.SZ"
    assert rows[1]["type"] == "stock"
    assert TDXAPIProvider().get_instruments("etf") == []


def test_symbol_helpers():
    assert tp._to_tdx_code("002491.SZ") == "sz002491"
    assert tp._to_tdx_code("000001.SH") == "sh000001"
    assert tp._to_tdx_code("399001.SZ") == "sz399001"
    assert tp._to_tdx_code("sz002491") == "sz002491"
    assert tp._to_app_symbol("002491", None) == "002491.SZ"
    assert tp._to_app_symbol("sh000001", None) == "000001.SH"
    assert tp._to_app_symbol("sz002491", None) == "002491.SZ"
    assert tp._to_app_symbol("600519.sh", None) == "600519.SH"
    assert tp._to_app_symbol("002491", 0) == "002491.SZ"
    assert tp._to_app_symbol("600519", "sh") == "600519.SH"
    assert tp._minute_type("5m") == "minute5"


def test_quote_row_maps_sh_index_close_as_last_price():
    row = TDXAPIProvider._quote_row({
        "Exchange": 1,
        "Code": "000001",
        "K": {"Last": 4041240, "Open": 4019490, "High": 4028510, "Low": 3971710, "Close": 3991330},
        "TotalHand": 496309842,
        "Amount": 1157660147712,
        "ServerTime": "14123182",
    }, {})

    assert row is not None
    assert row["symbol"] == "000001.SH"
    assert row["last_price"] == 3991.33
    assert row["prev_close"] == 4041.24
    assert abs(row["change_pct"] - ((3991.33 - 4041.24) / 4041.24)) < 1e-12


def test_plugin_discovered_in_loader():
    from app.data_providers import custom as cs

    plugins = {p["name"]: p for p in cs.list_plugins()}
    assert "tdxapi" in plugins
    assert plugins["tdxapi"]["runtime"] == "none"
    assert "daily" in plugins["tdxapi"]["datasets"]
    assert "minute" in plugins["tdxapi"]["datasets"]
    assert "realtime" in plugins["tdxapi"]["datasets"]
    assert cs.is_builtin("tdxapi")


def test_plugin_registered_when_available(monkeypatch):
    from app.data_providers import custom as cs
    from app.data_providers.custom import loader

    monkeypatch.setattr(loader, "_call_check", lambda ref: (True, "ok"))
    monkeypatch.setattr(loader, "_load_entry", _load_tdxapi_entry)
    loader._load_builtin_plugins()

    assert "tdxapi" in cs.names()
    assert cs.is_custom_provider("tdxapi")
    assert cs.provider_has_dataset("tdxapi", "daily")
    assert cs.provider_has_dataset("tdxapi", "minute")
    assert cs.provider_has_dataset("tdxapi", "realtime")
    assert not cs.provider_has_dataset("tdxapi", "financial")


def _load_tdxapi_entry(entry_ref: str):
    if "TDXAPIProvider" in entry_ref:
        from app.plugins.tdxapi.provider import TDXAPIProvider

        return TDXAPIProvider
    if "availability" in entry_ref:
        from app.plugins.tdxapi.provider import availability

        return availability
    raise ValueError(f"unknown entry: {entry_ref}")
