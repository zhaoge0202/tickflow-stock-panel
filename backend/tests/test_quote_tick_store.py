from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from app.services import quote_tick_store
from app.services.quote_service import QuoteService

CN = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 8)


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 7, 8, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def _ms_on(day: int, hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 7, day, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def _clear_hot_rows(data_dir) -> None:
    key = str(data_dir)
    with quote_tick_store._lock:
        quote_tick_store._rings.pop(key, None)
        quote_tick_store._buffers.pop(key, None)


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


def test_latest_uses_mysql_hot_cache_when_memory_ring_is_empty(tmp_path, monkeypatch):
    _clear_hot_rows(tmp_path)
    monkeypatch.setattr(
        quote_tick_store,
        "_mysql_latest",
        lambda target_date, symbols=None: [{
            "symbol": "002491.SZ",
            "trade_date": target_date.isoformat(),
            "event_ts": _ms(9, 30),
            "last_price": 10.3,
            "source": "tdxapi",
        }],
    )

    rows = quote_tick_store.latest(
        tmp_path,
        ["002491.SZ"],
        target_date=TRADE_DATE,
    )

    assert rows[0]["last_price"] == 10.3


def test_quote_tick_store_writes_when_late_numeric_columns_appear(tmp_path):
    rows = [
        {
            "symbol": f"000{i:03d}.SZ",
            "last_price": 10 + i / 100,
            "timestamp": _ms(10, 0, min(i % 60, 59)),
            "amount": None,
            "bid_depth_amount": None,
        }
        for i in range(120)
    ]
    rows.append({
        "symbol": "300750.SZ",
        "last_price": 319.1,
        "timestamp": _ms(10, 3, 0),
        "amount": 319_108_120.0,
        "bid_depth_amount": 319_108_120.0,
    })

    quote_tick_store.append_many(tmp_path, rows, source="tdxapi", force_flush=True)

    latest = quote_tick_store.latest(tmp_path, ["300750.SZ"], target_date=TRADE_DATE)
    assert latest[0]["amount"] == 319_108_120.0
    assert latest[0]["bid_depth_amount"] == 319_108_120.0


def test_symbol_queries_use_lazy_scan_without_caching_whole_partition(tmp_path, monkeypatch):
    quote_tick_store.append_many(tmp_path, [
        {
            "symbol": "002491.SZ",
            "last_price": 10.0,
            "volume": 100,
            "amount": 100_000,
            "timestamp": _ms(9, 30, 0),
        },
        {
            "symbol": "300750.SZ",
            "last_price": 319.1,
            "volume": 1_000,
            "amount": 319_100,
            "timestamp": _ms(9, 30, 1),
        },
        {
            "symbol": "002491.SZ",
            "last_price": 10.2,
            "volume": 130,
            "amount": 132_000,
            "timestamp": _ms(9, 30, 5),
        },
    ], source="tdxapi", force_flush=True)
    legacy_dir = tmp_path / "quote_ticks" / f"date={TRADE_DATE.isoformat()}" / "hour=09"
    pl.DataFrame({
        "symbol": ["002491.SZ"],
        "event_ts": [_ms(9, 30, 10)],
        "ingest_ts": [_ms(9, 30, 11)],
        "trade_date": [TRADE_DATE.isoformat()],
        "hour": ["09"],
        "last_price": [10.3],
    }).write_parquet(legacy_dir / "legacy-schema.parquet")
    _clear_hot_rows(tmp_path)

    scan_paths = []
    seen_symbols = []
    original_scan_parquet = quote_tick_store.pl.scan_parquet
    original_json_safe = quote_tick_store._json_safe

    def tracking_scan_parquet(path, *args, **kwargs):
        scan_paths.append(str(path))
        return original_scan_parquet(path, *args, **kwargs)

    def reject_eager_read(*args, **kwargs):
        raise AssertionError("指定 symbol 查询不应使用 eager read_parquet")

    def tracking_json_safe(row):
        if row.get("symbol"):
            seen_symbols.append(row["symbol"])
        return original_json_safe(row)

    monkeypatch.setattr(quote_tick_store.pl, "scan_parquet", tracking_scan_parquet)
    monkeypatch.setattr(quote_tick_store.pl, "read_parquet", reject_eager_read)
    monkeypatch.setattr(quote_tick_store, "_json_safe", tracking_json_safe)

    ticks = quote_tick_store.read_ticks(
        tmp_path,
        target_date=TRADE_DATE,
        symbols=["002491.SZ"],
    )
    latest = quote_tick_store.latest(
        tmp_path,
        ["002491.SZ"],
        target_date=TRADE_DATE,
    )
    bars = quote_tick_store.bars(
        tmp_path,
        "002491.SZ",
        freq="5s",
        target_date=TRADE_DATE,
    )

    assert scan_paths
    assert [row["last_price"] for row in ticks] == [10.0, 10.2, 10.3]
    assert {row["symbol"] for row in ticks} == {"002491.SZ"}
    assert latest[0]["symbol"] == "002491.SZ"
    assert latest[0]["last_price"] == 10.3
    assert bars[-1]["close"] == 10.3
    assert set(seen_symbols) == {"002491.SZ"}


def test_quote_service_realtime_frames_write_late_numeric_columns():
    records = [
        {
            "symbol": f"000{i:03d}.SZ",
            "name": f"测试{i}",
            "last_price": 10 + i / 100,
            "open": None,
            "high": None,
            "low": None,
            "volume": None,
            "amount": None,
            "prev_close": None,
            "change_pct": None,
            "change_amount": None,
            "amplitude": None,
            "turnover_rate": None,
        }
        for i in range(120)
    ]
    records.append({
        "symbol": "300750.SZ",
        "name": "宁德时代",
        "last_price": 319.1,
        "open": 318.0,
        "high": 321.0,
        "low": 317.0,
        "volume": 1_000_000.0,
        "amount": 319_108_120.0,
        "prev_close": 318.0,
        "change_pct": 0.00345,
        "change_amount": 1.1,
        "amplitude": 0.0125,
        "turnover_rate": 0.8,
    })

    daily = QuoteService._build_daily(records)
    extra = QuoteService._build_quote_extra(records)

    daily_row = daily.filter(pl.col("symbol") == "300750.SZ").row(0, named=True)
    extra_row = extra.filter(pl.col("symbol") == "300750.SZ").row(0, named=True)
    assert daily_row["amount"] == 319_108_120.0
    assert extra_row["change_amount"] == 1.1
