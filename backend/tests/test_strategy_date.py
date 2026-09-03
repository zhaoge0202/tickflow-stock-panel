from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import pytest

from app.market_time import latest_completed_strategy_date
from app.services.strategy_date import (
    latest_strategy_date,
    reject_intraday_strategy_date,
)


CN_TZ = timezone(timedelta(hours=8))


def test_strategy_date_excludes_today_before_cutoff() -> None:
    available = [date(2026, 9, 2), date(2026, 9, 3)]
    before_close = datetime(2026, 9, 3, 14, 59, tzinfo=CN_TZ)

    assert latest_completed_strategy_date(available, before_close) == date(2026, 9, 2)


def test_strategy_date_allows_today_after_cutoff() -> None:
    available = [date(2026, 9, 2), date(2026, 9, 3)]
    after_close = datetime(2026, 9, 3, 15, 30, tzinfo=CN_TZ)

    assert latest_completed_strategy_date(available, after_close) == date(2026, 9, 3)


def test_strategy_date_weekend_uses_latest_local_partition() -> None:
    available = [date(2026, 9, 3), date(2026, 9, 5)]
    weekend = datetime(2026, 9, 5, 10, 0, tzinfo=CN_TZ)

    assert latest_completed_strategy_date(available, weekend) == date(2026, 9, 3)


def test_latest_strategy_date_ignores_incomplete_partition(tmp_path) -> None:
    root = tmp_path / "kline_daily_enriched"
    (root / "date=2026-09-02").mkdir(parents=True)
    (root / "date=2026-09-02" / "part.parquet").write_bytes(b"ok")
    (root / "date=2026-09-03").mkdir()

    before_close = datetime(2026, 9, 3, 14, 0, tzinfo=CN_TZ)
    assert latest_strategy_date(tmp_path, now=before_close) == date(2026, 9, 2)


def test_explicit_today_is_rejected_before_cutoff() -> None:
    with pytest.raises(ValueError, match="尚未收盘"):
        reject_intraday_strategy_date(
            date(2026, 9, 3),
            now=datetime(2026, 9, 3, 14, 0, tzinfo=CN_TZ),
        )


def test_explicit_today_is_allowed_after_cutoff() -> None:
    reject_intraday_strategy_date(
        date(2026, 9, 3),
        now=datetime(2026, 9, 3, 15, 30, tzinfo=CN_TZ),
    )
