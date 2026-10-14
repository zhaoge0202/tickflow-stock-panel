from __future__ import annotations

from datetime import date

import polars as pl

from app.api.kline import _daily_rows_limit, _needs_live_daily_fill


def test_needs_live_daily_fill_when_local_range_is_short():
    df = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date(2026, 6, 10)],
        "close": [10.0],
    })

    assert _needs_live_daily_fill(df, date(2025, 7, 10)) is True


def test_needs_live_daily_fill_accepts_covered_range():
    df = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [date(2025, 7, 8), date(2026, 7, 10)],
        "close": [9.0, 10.0],
    })

    assert _needs_live_daily_fill(df, date(2025, 7, 10)) is False


def test_daily_rows_limit_uses_days_without_explicit_start_date():
    assert _daily_rows_limit(date(2026, 3, 12), date(2026, 7, 10), 120, False) == 120


def test_daily_rows_limit_expands_for_explicit_date_range():
    assert _daily_rows_limit(date(2025, 7, 10), date(2026, 7, 10), 120, True) == 366


def test_daily_rows_limit_is_capped():
    assert _daily_rows_limit(date(2018, 1, 1), date(2026, 7, 10), 120, True) == 2000
