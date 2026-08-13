"""TDXAPIProvider 归一化与插件注册测试。"""
from __future__ import annotations

import datetime as dt
import threading
import time

import polars as pl
import pytest

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


def test_get_minute_retries_transient_tdx_failure(monkeypatch):
    calls = 0

    def fake(self, method, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("获取K线失败: 超时")
        return {
            "list": [{
                "Time": "2026-07-10T09:31:00+08:00",
                "Open": 22900,
                "High": 23000,
                "Low": 22800,
                "Close": 22950,
                "Volume": 12,
                "Amount": 275400,
            }],
        }

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)

    df = TDXAPIProvider().get_minute(
        ["002491.SZ"],
        dt.datetime(2026, 7, 10, 9, 30),
        dt.datetime(2026, 7, 10, 15, 0),
    )

    assert calls == 3
    assert df.height == 1
    assert df["close"][0] == 22.95


def test_get_minute_raises_after_retry_exhausted(monkeypatch):
    def fake(self, method, path, **kwargs):
        raise RuntimeError("获取K线失败: 超时")

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)

    with pytest.raises(RuntimeError, match="重试 3 次"):
        TDXAPIProvider().get_minute(
            ["002491.SZ"],
            dt.datetime(2026, 7, 10, 9, 30),
            dt.datetime(2026, 7, 10, 15, 0),
        )


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


def test_get_realtime_fetches_batches_concurrently_and_keeps_order(monkeypatch):
    monkeypatch.setattr(tp, "_QUOTE_BATCH", 2)
    monkeypatch.setattr(tp, "_realtime_workers", lambda: 3)
    provider = TDXAPIProvider()
    monkeypatch.setattr(provider, "get_instruments", lambda _asset_type: [])

    running = 0
    max_running = 0
    lock = threading.Lock()

    def fake_fetch(chunk, _name_map):
        nonlocal running, max_running
        with lock:
            running += 1
            max_running = max(max_running, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return [{"symbol": symbol} for symbol in chunk]

    monkeypatch.setattr(provider, "_fetch_realtime_chunk", fake_fetch)

    rows = provider.get_realtime(symbols=["A", "B", "C", "D", "E"])

    assert max_running > 1
    assert [row["symbol"] for row in rows] == ["A", "B", "C", "D", "E"]


def test_get_realtime_maps_depth_and_activity_fields(monkeypatch):
    def fake_codes(kwargs):
        return {"codes": [{"code": "002491", "name": "通鼎互联", "exchange": "sz"}]}

    def fake_quote(kwargs):
        assert kwargs["json"]["codes"] == ["sz002491"]
        return [
            {
                "Exchange": 0,
                "Code": "002491",
                "K": {"Last": 9900, "Open": 9950, "High": 10100, "Low": 9900, "Close": 10000},
                "TotalHand": 100,
                "Amount": 1_000_000,
                "ServerTime": "1730617200",
                "BuyLevel": [
                    {"Buy": True, "Price": 9990, "Number": 100},
                    {"Buy": True, "Price": 9980, "Number": 90},
                    {"Buy": True, "Price": 9970, "Number": 80},
                    {"Buy": True, "Price": 9960, "Number": 70},
                    {"Buy": True, "Price": 9950, "Number": 60},
                ],
                "SellLevel": [
                    {"Buy": False, "Price": 10010, "Number": 50},
                    {"Buy": False, "Price": 10020, "Number": 40},
                    {"Buy": False, "Price": 10030, "Number": 30},
                    {"Buy": False, "Price": 10040, "Number": 20},
                    {"Buy": False, "Price": 10050, "Number": 10},
                ],
                "InsideDish": 300,
                "OuterDisc": 600,
                "Intuition": 88,
                "Rate": 0.8,
                "Active1": 12,
                "Active2": 34,
            },
        ]

    _patch_request(monkeypatch, {
        ("GET", "/api/codes"): fake_codes,
        ("POST", "/api/batch-quote"): fake_quote,
    })

    row = TDXAPIProvider().get_realtime(symbols=["002491.SZ"])[0]

    assert row["bid1"] == row["bid1_price"] == 9.99
    assert row["ask1"] == row["ask1_price"] == 10.01
    assert row["bid5_price"] == 9.95
    assert row["ask5_vol"] == 10
    assert abs(row["spread"] - 0.02) < 1e-12
    assert row["spread_pct"] == row["spread"] / 10.0
    assert row["bid_depth_vol"] == 400
    assert row["ask_depth_vol"] == 150
    assert row["bid_depth_amount"] > row["ask_depth_amount"]
    assert row["depth_imbalance"] > 0
    assert row["current_volume"] == 88
    assert row["inside_volume"] == 300
    assert row["outside_volume"] == 600
    assert row["outside_inside_ratio"] == 2
    assert row["active_net_volume"] == 300
    assert row["speed_rate"] == 0.8
    assert row["active1"] == 12
    assert row["active2"] == 34


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


def test_get_realtime_retries_batch_without_missing_code(monkeypatch):
    quote_calls = []

    def fake(self, method, path, **kwargs):
        if path == "/api/codes":
            return {"codes": []}
        codes = kwargs["json"]["codes"]
        quote_calls.append(codes)
        if "sh510143" in codes:
            raise RuntimeError("获取行情失败: 未查询到代码[sh510143]相关信息")
        return [
            {
                "Exchange": 0 if code.startswith("sz") else 1,
                "Code": code[2:],
                "K": {"Last": 1000, "Open": 1000, "High": 1010, "Low": 990, "Close": 1005},
                "TotalHand": 10,
                "Amount": 100000,
                "ServerTime": "1730617200",
            }
            for code in codes
        ]

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)

    rows = TDXAPIProvider().get_realtime(
        symbols=["159881.SZ", "510143.SH", "510660.SH"],
    )

    assert quote_calls == [
        ["sz159881", "sh510143", "sh510660"],
        ["sz159881", "sh510660"],
    ]
    assert [row["symbol"] for row in rows] == ["159881.SZ", "510660.SH"]


def test_get_realtime_caches_missing_quote_code(monkeypatch):
    quote_calls = []

    def fake(self, method, path, **kwargs):
        if path == "/api/codes":
            return {"codes": []}
        codes = kwargs["json"]["codes"]
        quote_calls.append(codes)
        if "sh510143" in codes:
            raise RuntimeError("获取行情失败: 未查询到代码[sh510143]相关信息")
        return [
            {
                "Exchange": 0 if code.startswith("sz") else 1,
                "Code": code[2:],
                "K": {"Last": 1000, "Open": 1000, "High": 1010, "Low": 990, "Close": 1005},
                "TotalHand": 10,
                "Amount": 100000,
                "ServerTime": "1730617200",
            }
            for code in codes
        ]

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)
    provider = TDXAPIProvider()

    first = provider.get_realtime(symbols=["159881.SZ", "510143.SH", "510660.SH"])
    second = provider.get_realtime(symbols=["159881.SZ", "510143.SH", "510660.SH"])

    assert [row["symbol"] for row in first] == ["159881.SZ", "510660.SH"]
    assert [row["symbol"] for row in second] == ["159881.SZ", "510660.SH"]
    assert quote_calls == [
        ["sz159881", "sh510143", "sh510660"],
        ["sz159881", "sh510660"],
        ["sz159881", "sh510660"],
    ]


def test_server_time_parses_tdx_hhmmss_milliseconds():
    timestamp = tp._server_time_ms("143523199")
    assert timestamp is not None

    decoded = dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.timezone(dt.timedelta(hours=8)))
    assert (decoded.hour, decoded.minute, decoded.second) == (14, 35, 23)
    assert decoded.microsecond == 199_000


def test_get_realtime_maps_preopen_auction_reference(monkeypatch):
    def fake_codes(kwargs):
        return {"codes": [{"code": "002491", "name": "通鼎互联", "exchange": "sz"}]}

    def fake_quote(kwargs):
        assert kwargs["json"]["codes"] == ["sz002491"]
        return [
            {
                "Exchange": 0,
                "Code": "002491",
                "K": {"Last": 22610, "Open": 0, "High": 0, "Low": 0, "Close": 0},
                "TotalHand": 0,
                "Amount": 5.877471754111438e-39,
                "ServerTime": "9203601",
                "BuyLevel": [
                    {"Buy": True, "Price": 22660, "Number": 1111},
                    {"Buy": True, "Price": 0, "Number": 930},
                ],
                "SellLevel": [
                    {"Buy": False, "Price": 22660, "Number": 1111},
                    {"Buy": False, "Price": 0, "Number": 0},
                ],
            },
        ]

    _patch_request(monkeypatch, {
        ("GET", "/api/codes"): fake_codes,
        ("POST", "/api/batch-quote"): fake_quote,
    })

    row = TDXAPIProvider().get_realtime(symbols=["002491.SZ"])[0]

    assert row["price_type"] == "auction_reference"
    assert row["market_phase"] == "preopen_auction"
    assert row["last_price"] == 22.66
    assert row["auction_price"] == 22.66
    assert row["auction_matched_volume"] == 1111
    assert row["auction_unmatched_side"] == "buy"
    assert row["auction_unmatched_volume"] == 930
    assert row["auction_unmatched_ratio"] == 930 / (1111 + 930)
    assert row["auction_pressure_score"] == row["auction_unmatched_ratio"]
    assert row["amount"] is None


def test_get_market_breadth_maps_stats_and_major_indices(monkeypatch):
    def fake_stats(kwargs):
        return {
            "sh": {"total": 3, "up": 2, "down": 1, "flat": 0},
            "sz": {"total": 3, "up": 1, "down": 2, "flat": 0},
            "bj": {"total": 1, "up": 1, "down": 0, "flat": 0},
            "update_time": "2026-07-08T10:00:00+08:00",
        }

    def fake_quote(kwargs):
        assert "sh000001" in kwargs["json"]["codes"]
        return [
            {
                "Exchange": 1,
                "Code": "000001",
                "K": {"Last": 3200000, "Open": 3205000, "High": 3210000, "Low": 3190000, "Close": 3216000},
                "TotalHand": 10,
                "Amount": 1000000,
                "ServerTime": "1783485600",
            },
        ]

    _patch_request(monkeypatch, {
        ("GET", "/api/market-stats"): fake_stats,
        ("POST", "/api/batch-quote"): fake_quote,
    })

    snapshot = TDXAPIProvider().get_market_breadth(major_symbols=["000001.SH"])

    assert snapshot["up_count"] == 4
    assert snapshot["down_count"] == 3
    assert snapshot["flat_count"] == 0
    assert snapshot["total_count"] == 7
    assert snapshot["up_down_ratio"] == 4 / 3
    assert snapshot["market_temperature"] == "warm"
    assert snapshot["major_indices"][0]["symbol"] == "000001.SH"
    assert snapshot["major_index_change_pct"] == snapshot["major_indices"][0]["change_pct"]


def test_get_instruments_supports_tdx_etf_and_core_index(monkeypatch):
    def fake_etf(kwargs):
        return {
            "list": [
                {"code": "589020", "name": "科创半导体设备ETF鹏华", "exchange": "sh", "last_price": 3.42},
            ],
        }

    def fake_quote(kwargs):
        return [
            {
                "Exchange": 1,
                "Code": "000001",
                "K": {"Last": 3200000, "Open": 3205000, "High": 3210000, "Low": 3190000, "Close": 3216000},
                "TotalHand": 10,
                "Amount": 1000000,
                "ServerTime": "1783485600",
            },
        ]

    _patch_request(monkeypatch, {
        ("GET", "/api/etf"): fake_etf,
        ("POST", "/api/batch-quote"): fake_quote,
    })

    provider = TDXAPIProvider()
    etfs = provider.get_instruments("etf")
    indices = provider.get_instruments("index")

    assert etfs[0]["symbol"] == "589020.SH"
    assert etfs[0]["name"] == "科创半导体设备ETF鹏华"
    assert etfs[0]["asset_type"] == "etf"
    assert any(row["symbol"] == "000001.SH" and row["asset_type"] == "index" for row in indices)


def test_get_trade_ticks_normalizes_recent_rows(monkeypatch):
    def fake_trade(kwargs):
        assert kwargs["params"] == {"code": "sz002491", "date": "20260707"}
        return {
            "Count": 2,
            "List": [
                {
                    "Time": "2026-07-07T09:25:00+08:00",
                    "Price": 10460,
                    "Volume": 5586,
                    "Status": 2,
                    "Number": 269,
                },
                {
                    "Time": "2026-07-07T15:28:00+08:00",
                    "Price": 10470,
                    "Volume": 19,
                    "Status": 5,
                    "Number": 1,
                },
            ],
        }

    _patch_request(monkeypatch, {
        ("GET", "/api/trade"): fake_trade,
    })

    rows = TDXAPIProvider().get_trade_ticks("002491.SZ", dt.date(2026, 7, 7), mode="recent")

    assert len(rows) == 2
    assert rows[0]["symbol"] == "002491.SZ"
    assert rows[0]["trade_date"] == dt.date(2026, 7, 7)
    assert rows[0]["datetime"] == dt.datetime(2026, 7, 7, 9, 25)
    assert rows[0]["seq_in_day"] == 1
    assert rows[0]["price"] == 10.46
    assert rows[0]["volume"] == 5586
    assert rows[0]["amount"] == 10.46 * 5586 * 100
    assert rows[0]["side"] == "neutral"
    assert rows[0]["side_label"] == "中性"
    assert rows[1]["raw_status"] == 5
    assert rows[1]["side"] == "unknown"


def test_get_trade_ticks_today_omits_date_for_live_endpoint(monkeypatch):
    monkeypatch.setattr(tp, "_cn_today", lambda: dt.date(2026, 7, 8))

    def fake_trade(kwargs):
        assert kwargs["params"] == {"code": "sz000725"}
        return {
            "Count": 1,
            "List": [{
                "Time": "2026-07-08T09:30:00+08:00",
                "Price": 7690,
                "Volume": 96668,
                "Status": 1,
                "Number": 2609,
            }],
        }

    _patch_request(monkeypatch, {
        ("GET", "/api/trade"): fake_trade,
    })

    rows = TDXAPIProvider().get_trade_ticks("000725.SZ", dt.date(2026, 7, 8), mode="recent")

    assert len(rows) == 1
    assert rows[0]["symbol"] == "000725.SZ"
    assert rows[0]["trade_date"] == dt.date(2026, 7, 8)


def test_get_trade_ticks_all_uses_minute_trade_all(monkeypatch):
    def fake_all(kwargs):
        assert kwargs["params"] == {"code": "sz002491"}
        return {
            "Count": 1,
            "List": [{
                "Time": "2026-07-07T13:21:00+08:00",
                "Price": 10450,
                "Volume": 1,
                "Status": 1,
                "Number": 1,
            }],
        }

    _patch_request(monkeypatch, {
        ("GET", "/api/minute-trade-all"): fake_all,
    })

    rows = TDXAPIProvider().get_trade_ticks("002491.SZ", mode="all", limit=None)

    assert len(rows) == 1
    assert rows[0]["side"] == "sell"
    assert rows[0]["order_count"] == 1


def test_get_trade_history_full_normalizes_lowercase_rows(monkeypatch):
    def fake_history(kwargs):
        assert kwargs["params"] == {
            "code": "sz002491",
            "start_date": "20260707",
            "end_date": "20260707",
            "limit": 2,
        }
        return {
            "code": "002491",
            "count": 2,
            "list": [
                {
                    "time": "2026-07-07T09:30:00+08:00",
                    "price": 10.46,
                    "volume": 100,
                    "status": 1,
                    "number": 3,
                },
                {
                    "time": "2026-07-07T09:31:00+08:00",
                    "price": 10.5,
                    "volume": 20,
                    "status": 2,
                    "number": 1,
                },
            ],
        }

    _patch_request(monkeypatch, {
        ("GET", "/api/trade-history/full"): fake_history,
    })

    rows = TDXAPIProvider().get_trade_history_full(
        "002491.SZ",
        start_date=dt.date(2026, 7, 7),
        end_date=dt.date(2026, 7, 7),
        limit=2,
    )

    assert len(rows) == 2
    assert rows[0]["symbol"] == "002491.SZ"
    assert rows[0]["trade_date"] == dt.date(2026, 7, 7)
    assert rows[0]["datetime"] == dt.datetime(2026, 7, 7, 9, 30)
    assert rows[0]["price"] == 10.46
    assert rows[0]["amount"] == 10.46 * 100 * 100
    assert rows[0]["side"] == "sell"
    assert rows[0]["order_count"] == 3
    assert rows[0]["source"] == "tdxapi_trade_history_minute_precision"
    assert rows[1]["side"] == "neutral"


def test_get_auction_results_filters_0925_from_history_trades(monkeypatch):
    def fake_history(kwargs):
        assert kwargs["params"] == {
            "code": "sz002491",
            "start_date": "20260707",
            "end_date": "20260707",
        }
        return {
            "code": "002491",
            "count": 3,
            "list": [
                {
                    "time": "2026-07-07T09:24:00+08:00",
                    "price": 10.4,
                    "volume": 20,
                    "status": 1,
                    "number": 2,
                },
                {
                    "time": "2026-07-07T09:25:00+08:00",
                    "price": 10.46,
                    "volume": 5586,
                    "status": 2,
                    "number": 269,
                },
                {
                    "time": "2026-07-07T09:30:00+08:00",
                    "price": 10.5,
                    "volume": 100,
                    "status": 0,
                    "number": 8,
                },
            ],
        }

    _patch_request(monkeypatch, {
        ("GET", "/api/trade-history/full"): fake_history,
    })

    rows = TDXAPIProvider().get_auction_results(["002491.SZ"], dt.date(2026, 7, 7))

    assert len(rows) == 1
    assert rows[0]["symbol"] == "002491.SZ"
    assert rows[0]["trade_date"] == dt.date(2026, 7, 7)
    assert rows[0]["auction_time"] == "09:25"
    assert rows[0]["auction_datetime"] == dt.datetime(2026, 7, 7, 9, 25)
    assert rows[0]["price"] == 10.46
    assert rows[0]["volume"] == 5586
    assert rows[0]["amount"] == 10.46 * 5586 * 100
    assert rows[0]["order_count"] == 269
    assert rows[0]["trade_index"] == 2
    assert rows[0]["source"] == "tdxapi_auction_result_history"
    assert rows[0]["source_trade_tick"] == "tdxapi_trade_history_minute_precision"


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
        ("GET", "/api/etf-codes"): {"count": 0, "list": []},
    })

    rows = TDXAPIProvider().get_instruments("stock")
    assert rows[0]["symbol"] == "600519.SH"
    assert rows[1]["symbol"] == "002491.SZ"
    assert rows[1]["type"] == "stock"
    assert TDXAPIProvider().get_instruments("etf") == []


def _tdx_finance_snapshot() -> dict:
    return {
        "Market": 1,
        "Code": "600519",
        "LiuTongGuBen": 1256197800.0,
        "Province": 24,
        "Industry": 36,
        "UpdatedDate": 20260331,
        "IPODate": 20010827,
        "ZongGuBen": 1256197800.0,
        "ZongZiChan": 300000000000.0,
        "LiuDongZiChan": 200000000000.0,
        "GuDingZiChan": 10000000000.0,
        "WuXingZiChan": 1000000000.0,
        "GuDongRenShu": 180000.0,
        "LiuDongFuZhai": 50000000000.0,
        "ChangQiFuZhai": 10000000000.0,
        "ZiBenGongJiJin": 1000000000.0,
        "JingZiChan": 200000000000.0,
        "ZhuYingShouRu": 120000000000.0,
        "ZhuYingLiRun": 90000000000.0,
        "YingShouZhangKuan": 1000000000.0,
        "YingYeLiRun": 80000000000.0,
        "TouZiShouYi": 100000000.0,
        "JingYingXianJinLiu": 70000000000.0,
        "ZongXianJinLiu": 60000000000.0,
        "CunHuo": 40000000000.0,
        "LiRunZongHe": 85000000000.0,
        "ShuiHouLiRun": 65000000000.0,
        "JingLiRun": 65000000000.0,
        "WeiFenLiRun": 100000000000.0,
    }


@pytest.mark.parametrize(
    ("table", "expected_field", "expected_value"),
    [
        ("metrics", "net_profit", 65000000000.0),
        ("income", "main_revenue", 120000000000.0),
        ("balance_sheet", "inventory", 40000000000.0),
        ("cash_flow", "operating_cash_flow", 70000000000.0),
        ("shares", "float_shares", 1256197800.0),
    ],
)
def test_get_financials_maps_tdx_finance_snapshot(monkeypatch, table, expected_field, expected_value):
    calls = []

    def fake(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _tdx_finance_snapshot()

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)

    df = TDXAPIProvider().get_financials(table, ["600519.SH"])

    assert calls[0][0] == "GET"
    assert calls[0][1] == "/api/finance"
    assert calls[0][2]["params"] == {"code": "sh600519"}
    assert df.height == 1
    assert df["symbol"][0] == "600519.SH"
    assert df["source"][0] == "tdxapi"
    assert df["table"][0] == table
    assert df["report_date"][0] == dt.date(2026, 3, 31)
    assert df["period_end"][0] == "2026-03-31"
    assert df["announce_date"][0] == "2026-03-31"
    assert df["ipo_date"][0] == dt.date(2001, 8, 27)
    assert df[expected_field][0] == expected_value
    # 前端契约字段: 从 TDX 快照派生, 避免页面大面积 "—"
    if table == "metrics":
        assert abs(df["eps_basic"][0] - (65000000000.0 / 1256197800.0)) < 1e-6
        assert abs(df["roe"][0] - (65000000000.0 / 200000000000.0 * 100)) < 1e-6
        assert df["revenue"][0] == 120000000000.0
        assert df["net_income"][0] == 65000000000.0
    elif table == "income":
        assert df["revenue"][0] == 120000000000.0
        assert df["net_income"][0] == 65000000000.0
    elif table == "balance_sheet":
        assert df["total_assets"][0] == 300000000000.0
        assert df["total_equity"][0] == 200000000000.0
        assert df["total_liabilities"][0] == 60000000000.0
    elif table == "cash_flow":
        assert df["net_operating_cash_flow"][0] == 70000000000.0
        assert df["net_cash_change"][0] == 60000000000.0
    elif table == "shares":
        # 上游 historical shares 契约: symbol/period_end/float_shares (+ total_shares)
        assert df["total_shares"][0] == 1256197800.0
        assert {"symbol", "period_end", "float_shares"} <= set(df.columns)


def test_get_financials_shares_rejects_missing_float(monkeypatch):
    def fake(self, method, path, **kwargs):
        snap = _tdx_finance_snapshot()
        snap["LiuTongGuBen"] = 0
        return snap

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)
    df = TDXAPIProvider().get_financials("shares", ["600519.SH"])
    assert df.is_empty()


def test_get_minute_accepts_asset_type_before_freq(monkeypatch):
    """对齐上游 MarketDataProvider 签名: asset_type, freq, on_chunk_done。"""
    progress: list[tuple[int, int]] = []

    def fake(self, method, path, **kwargs):
        return {
            "list": [{
                "Time": "2026-01-05T09:31:00+08:00",
                "Open": 10000,
                "High": 10000,
                "Low": 10000,
                "Close": 10000,
                "Volume": 1,
                "Amount": 1,
            }],
        }

    monkeypatch.setattr(TDXAPIProvider, "_request", fake)
    df = TDXAPIProvider().get_minute(
        ["002491.SZ"],
        dt.datetime(2026, 1, 5, 9, 30),
        dt.datetime(2026, 1, 5, 15, 0),
        asset_type="etf",
        freq="1m",
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    )
    assert df.height == 1
    assert progress == [(1, 1)]


def test_symbol_helpers():
    assert tp._to_tdx_code("002491.SZ") == "sz002491"
    assert tp._to_tdx_code("000001.SH") == "sh000001"
    assert tp._to_tdx_code("399001.SZ") == "sz399001"
    assert tp._to_tdx_code("sz002491") == "sz002491"
    assert tp._to_tdx_code("589020") == "sh589020"
    assert tp._to_tdx_code("159915") == "sz159915"
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


def test_server_time_decodes_tdx_compact_time(monkeypatch):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 7, 8, 18, 0, tzinfo=tz)

    monkeypatch.setattr(tp, "datetime", FixedDateTime)

    ts = tp._server_time_ms("15329240")
    decoded = dt.datetime.fromtimestamp(ts / 1000, tz=tp._CN_TZ)

    assert decoded.date() == dt.date(2026, 7, 8)
    assert decoded.hour == 15
    assert decoded.minute == 32
    assert decoded.second == 55


def test_server_time_moves_future_compact_time_to_previous_weekday(monkeypatch):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 7, 9, 0, 26, tzinfo=tz)

    monkeypatch.setattr(tp, "datetime", FixedDateTime)

    ts = tp._server_time_ms("15329240")
    decoded = dt.datetime.fromtimestamp(ts / 1000, tz=tp._CN_TZ)

    assert decoded.date() == dt.date(2026, 7, 8)
    assert decoded.hour == 15
    assert decoded.minute == 32


def test_server_time_moves_weekend_compact_time_to_previous_weekday(monkeypatch):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 7, 11, 10, 0, tzinfo=tz)

    monkeypatch.setattr(tp, "datetime", FixedDateTime)

    ts = tp._server_time_ms("9300000")
    decoded = dt.datetime.fromtimestamp(ts / 1000, tz=tp._CN_TZ)

    assert decoded.date() == dt.date(2026, 7, 10)
    assert decoded.hour == 9
    assert decoded.minute == 30


def test_server_time_keeps_unix_seconds():
    assert tp._server_time_ms("1730617200") == 1730617200 * 1000


def test_server_time_rejects_invalid_future_compact_value(monkeypatch):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 7, 8, 18, 0, tzinfo=tz)

    monkeypatch.setattr(tp, "datetime", FixedDateTime)

    assert tp._server_time_ms("9251501000") is None


def test_plugin_discovered_in_loader():
    from app.data_providers import custom as cs

    plugins = {p["name"]: p for p in cs.list_plugins()}
    assert "tdxapi" in plugins
    assert plugins["tdxapi"]["runtime"] == "none"
    assert "daily" in plugins["tdxapi"]["datasets"]
    assert "minute" in plugins["tdxapi"]["datasets"]
    assert "realtime" in plugins["tdxapi"]["datasets"]
    assert "trade_ticks" in plugins["tdxapi"]["datasets"]
    assert "auction_result" in plugins["tdxapi"]["datasets"]
    assert "financial" in plugins["tdxapi"]["datasets"]
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
    assert cs.provider_has_dataset("tdxapi", "trade_ticks")
    assert cs.provider_has_dataset("tdxapi", "auction_result")
    assert cs.provider_has_dataset("tdxapi", "financial")


def _load_tdxapi_entry(entry_ref: str):
    if "TDXAPIProvider" in entry_ref:
        from app.plugins.tdxapi.provider import TDXAPIProvider

        return TDXAPIProvider
    if "availability" in entry_ref:
        from app.plugins.tdxapi.provider import availability

        return availability
    raise ValueError(f"unknown entry: {entry_ref}")
