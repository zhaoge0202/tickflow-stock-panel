from __future__ import annotations

from datetime import date

import polars as pl

from app.services.quote_service import QuoteService
from app.tickflow.repository import KlineRepository


def test_build_daily_keeps_last_quote_for_duplicate_symbol():
    df = QuoteService._build_daily([
        {
            "symbol": "002491.SZ",
            "last_price": 12.1,
            "open": 12.0,
            "high": 12.2,
            "low": 11.9,
            "volume": 1,
            "amount": 100,
        },
        {
            "symbol": "002491.SZ",
            "last_price": 12.5,
            "open": 12.3,
            "high": 12.6,
            "low": 12.2,
            "volume": 2,
            "amount": 200,
        },
    ])

    assert df.height == 1
    assert df["symbol"][0] == "002491.SZ"
    assert df["close"][0] == 12.5
    assert df["volume"][0] == 2


def test_repository_dedupe_symbol_date_keeps_last_row():
    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ", "B.SZ"],
        "date": [date(2026, 7, 7), date(2026, 7, 7), date(2026, 7, 7)],
        "close": [1.0, 2.0, 3.0],
    })

    deduped = KlineRepository._dedupe_symbol_date(df, "test")

    assert deduped.height == 2
    rows = {row["symbol"]: row["close"] for row in deduped.to_dicts()}
    assert rows == {"A.SZ": 2.0, "B.SZ": 3.0}


def test_quote_service_shutdown_stop_preserves_realtime_preference(monkeypatch):
    saved: list[bool] = []
    monkeypatch.setattr(
        QuoteService,
        "_save_enabled",
        staticmethod(lambda enabled: saved.append(enabled)),
    )

    qs = QuoteService()
    qs.stop(persist_enabled=False)

    assert saved == []

    qs.disable()

    assert saved == [False]
