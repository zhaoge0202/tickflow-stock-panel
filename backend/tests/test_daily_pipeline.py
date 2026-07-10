from __future__ import annotations

from datetime import date, datetime

from app.jobs import daily_pipeline
from app.market_time import CN_TZ


def test_daily_batch_end_date_excludes_today_before_close(monkeypatch):
    monkeypatch.setattr(
        daily_pipeline,
        "cn_now",
        lambda: datetime(2026, 7, 10, 9, 30, tzinfo=CN_TZ),
    )

    assert daily_pipeline._daily_batch_end_date(date(2026, 7, 10)) == date(2026, 7, 9)


def test_daily_batch_end_date_allows_today_after_cutoff(monkeypatch):
    monkeypatch.setattr(
        daily_pipeline,
        "cn_now",
        lambda: datetime(2026, 7, 10, 15, 30, tzinfo=CN_TZ),
    )

    assert daily_pipeline._daily_batch_end_date(date(2026, 7, 10)) == date(2026, 7, 10)
