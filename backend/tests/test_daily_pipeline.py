from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

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


def test_run_minute_sync_is_a_separate_stage(monkeypatch, tmp_path):
    class _Capset:
        def has(self, capability):
            return capability.value == "kline.minute.batch"

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.setattr(daily_pipeline._prefs, "get_minute_sync_enabled", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_minute_sync_days", lambda: 5)
    monkeypatch.setattr(daily_pipeline, "_resolve_minute_symbols", lambda *_args: ["600000.SH"])
    monkeypatch.setattr(
        daily_pipeline.kline_sync,
        "sync_and_persist_minute",
        lambda *_args, **_kwargs: 240,
    )

    result = daily_pipeline.run_minute_sync(repo, _Capset())

    assert result["minute_rows"] == 240
    assert result["universe_size"] == 1
