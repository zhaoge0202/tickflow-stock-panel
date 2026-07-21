from datetime import date
from pathlib import Path

import polars as pl

from app.services import instrument_sync as ins


def test_merge_instrument_rows_fills_missing_metadata_from_tickflow():
    provider_rows = [
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "code": "002491",
            "exchange": "SZ",
            "region": "CN",
            "type": "stock",
            "listing_date": None,
            "total_shares": None,
            "float_shares": None,
            "tick_size": None,
            "limit_up": None,
            "limit_down": None,
        },
    ]
    tickflow_rows = [
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "code": "002491",
            "exchange": "SZ",
            "region": "CN",
            "type": "stock",
            "listing_date": "2011-05-18",
            "total_shares": 7638514276.0,
            "float_shares": 4386387432.0,
            "tick_size": 0.01,
            "limit_up": 26.54,
            "limit_down": 21.72,
        },
    ]

    merged = ins._merge_instrument_rows(provider_rows, tickflow_rows)

    assert len(merged) == 1
    row = merged[0]
    assert row["symbol"] == "002491.SZ"
    assert row["name"] == "通鼎互联"
    assert row["float_shares"] == 4386387432.0
    assert row["total_shares"] == 7638514276.0
    assert row["limit_up"] == 26.54
    assert row["limit_down"] == 21.72


def test_sanitize_limit_prices_rewrites_stale_provider_values():
    # 昨收 35.14 → 理论涨停 38.65; 上游给 42.91 (脏值) 应被重写
    rows = [
        {
            "symbol": "000021.SZ",
            "name": "深科技",
            "limit_up": 42.91,
            "limit_down": 35.11,
        },
        {
            "symbol": "300001.SZ",
            "name": "特锐德",
            "limit_up": 24.0,   # 正好等于 20.0 * 1.2
            "limit_down": 16.0,
        },
        {
            "symbol": "999999.SH",
            "name": "无昨收",
            "limit_up": 10.0,
            "limit_down": 8.0,
        },
    ]
    prev = {
        "000021.SZ": 35.14,
        "300001.SZ": 20.0,
    }
    out, stats = ins.sanitize_limit_prices(
        rows,
        prev_close_by_symbol=prev,
        as_of=date(2026, 7, 21),
        base_date=date(2026, 7, 20),
    )
    by_symbol = {r["symbol"]: r for r in out}

    assert by_symbol["000021.SZ"]["limit_up"] == 38.65
    assert by_symbol["000021.SZ"]["limit_down"] == 31.63
    assert by_symbol["000021.SZ"]["limit_source"] == "theoretical"
    assert by_symbol["000021.SZ"]["limit_base_date"] == "2026-07-20"

    assert by_symbol["300001.SZ"]["limit_up"] == 24.0
    assert by_symbol["300001.SZ"]["limit_down"] == 16.0
    assert by_symbol["300001.SZ"]["limit_source"] == "provider"

    assert by_symbol["999999.SH"]["limit_up"] is None
    assert by_symbol["999999.SH"]["limit_down"] is None
    assert by_symbol["999999.SH"]["limit_source"] == "missing_prev_close"

    assert stats["rewritten"] == 1
    assert stats["kept_provider"] == 1
    assert stats["cleared"] == 1


def test_sync_instruments_sanitizes_limits_before_write(tmp_path, monkeypatch):
    daily = tmp_path / "kline_daily" / "date=2026-07-20" / "part.parquet"
    daily.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000021.SZ"],
        "close": [35.14],
    }).write_parquet(daily)

    monkeypatch.setattr(ins, "cn_today", lambda: date(2026, 7, 21))
    monkeypatch.setattr(ins, "_fetch_instruments_via_provider", lambda: [
        {
            "symbol": "000021.SZ",
            "name": "深科技",
            "code": "000021",
            "exchange": "SZ",
            "region": "CN",
            "type": "stock",
            "listing_date": None,
            "total_shares": 1.0,
            "float_shares": 1.0,
            "tick_size": 0.01,
            "limit_up": 42.91,
            "limit_down": 35.11,
        },
    ])
    monkeypatch.setattr(ins, "_fetch_instruments_via_tickflow", lambda: [])

    n = ins.sync_instruments(tmp_path)
    assert n == 1
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    row = df.to_dicts()[0]
    assert row["limit_up"] == 38.65
    assert row["limit_down"] == 31.63
    assert row["limit_source"] == "theoretical"
    assert row["limit_base_date"] == "2026-07-20"
    assert str(row["as_of"]) == "2026-07-21"
