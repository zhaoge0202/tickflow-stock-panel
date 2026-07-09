from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services import quote_tick_store

CN = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 8)


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 7, 8, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def _ms_on(day: int, hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 7, day, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def test_quote_tick_store_appends_latest_bars_and_quality(tmp_path):
    rows = [
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "last_price": 10.0,
            "prev_close": 9.8,
            "open": 9.9,
            "high": 10.1,
            "low": 9.8,
            "volume": 100,
            "amount": 100_000,
            "timestamp": _ms(9, 30, 0),
        },
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "last_price": 10.2,
            "prev_close": 9.8,
            "open": 9.9,
            "high": 10.2,
            "low": 9.8,
            "volume": 130,
            "amount": 132_000,
            "timestamp": _ms(9, 30, 5),
            "bid1_price": 10.19,
            "bid1_vol": 200,
            "bid2_price": 10.18,
            "bid2_vol": 150,
            "ask1_price": 10.21,
            "ask1_vol": 80,
            "ask2_price": 10.22,
            "ask2_vol": 60,
            "spread_pct": 0.00196,
            "bid_depth_amount": 356_500,
            "ask_depth_amount": 143_000,
            "depth_imbalance": 0.427,
            "inside_volume": 300,
            "outside_volume": 600,
            "outside_inside_ratio": 2.0,
            "active_net_volume": 300,
            "current_volume": 88,
            "speed_rate": 0.8,
        },
    ]

    summary = quote_tick_store.append_many(tmp_path, rows, source="tdxapi", force_flush=True)
    latest = quote_tick_store.latest(tmp_path, ["002491.SZ"], target_date=TRADE_DATE)
    bars = quote_tick_store.bars(tmp_path, "002491.SZ", freq="5s", target_date=TRADE_DATE)
    quality = quote_tick_store.quality(tmp_path, ["002491.SZ", "300750.SZ"], target_date=TRADE_DATE)

    assert summary["source"] == "tdxapi"
    assert latest[0]["symbol"] == "002491.SZ"
    assert latest[0]["last_price"] == 10.2
    assert latest[0]["source"] == "tdxapi"
    assert latest[0]["bid1"] == 10.19
    assert latest[0]["bid1_price"] == 10.19
    assert latest[0]["ask2_price"] == 10.22
    assert latest[0]["depth_imbalance"] == 0.427
    assert latest[0]["outside_inside_ratio"] == 2.0
    raw = json.loads(latest[0]["raw"]) if latest[0].get("raw") else {}
    assert "bid1_price" not in raw
    assert "outside_inside_ratio" not in raw
    assert bars
    assert bars[-1]["close"] == 10.2
    assert quality["source"] == "tdxapi"
    assert quality["missing_symbols"] == ["300750.SZ"]


def test_latest_can_read_historical_date_without_duplicate_rows(tmp_path):
    quote_tick_store.append_many(tmp_path, [
        {"symbol": "002491.SZ", "last_price": 9.9, "timestamp": _ms_on(7, 14, 50)},
        {"symbol": "002491.SZ", "last_price": 10.1, "timestamp": _ms_on(8, 9, 35)},
    ], source="tdxapi", force_flush=True)

    latest_7 = quote_tick_store.latest(tmp_path, ["002491.SZ"], target_date=datetime(2026, 7, 7, tzinfo=CN).date())
    ticks_8 = quote_tick_store.read_ticks(tmp_path, target_date=datetime(2026, 7, 8, tzinfo=CN).date(), symbols=["002491.SZ"])

    assert latest_7[0]["last_price"] == 9.9
    assert [row["last_price"] for row in ticks_8] == [10.1]
