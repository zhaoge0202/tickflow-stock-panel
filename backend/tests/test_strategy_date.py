from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import market_time
from app.market_time import latest_completed_strategy_date
from app.services import strategy_date
from app.services.strategy_date import (
    latest_strategy_date,
    reject_intraday_strategy_date,
)

CN_TZ = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _isolated_date_cache():
    strategy_date._date_cache.clear()
    yield
    strategy_date._date_cache.clear()


def _partition(root, day: str, *, complete: bool = True):
    folder = root / f"date={day}"
    folder.mkdir(parents=True, exist_ok=True)
    if complete:
        (folder / "part.parquet").write_bytes(b"ok")
    return folder


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


def test_cached_dates_reapply_cutoff_without_rescanning(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    _partition(root, "2026-11-17")
    clock = [datetime(2026, 11, 17, 15, 29, 59, tzinfo=CN_TZ)]
    monkeypatch.setattr(market_time, "cn_now", lambda: clock[0])
    monkeypatch.setattr(strategy_date, "cn_now", lambda: clock[0])
    scans = []
    original = strategy_date.enriched_partition_dates

    def scan(*args):
        scans.append(1)
        return original(*args)

    monkeypatch.setattr(strategy_date, "enriched_partition_dates", scan)
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
    clock[0] = datetime(2026, 11, 17, 15, 30, 0, tzinfo=CN_TZ)
    assert latest_strategy_date(tmp_path) == date(2026, 11, 17)
    assert scans == [1]


def test_cached_dates_reapply_day_change(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    _partition(root, "2026-11-17")
    clock = [datetime(2026, 11, 16, 23, 59, 59, tzinfo=CN_TZ)]
    monkeypatch.setattr(market_time, "cn_now", lambda: clock[0])
    monkeypatch.setattr(strategy_date, "cn_now", lambda: clock[0])
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
    clock[0] = datetime(2026, 11, 17, 15, 30, 0, tzinfo=CN_TZ)
    assert latest_strategy_date(tmp_path) == date(2026, 11, 17)


def test_new_partition_invalidates_cached_dates(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    monkeypatch.setattr(strategy_date, "cn_now", lambda: datetime(2026, 11, 17, 16, 0, tzinfo=CN_TZ))
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
    _partition(root, "2026-11-17")
    assert latest_strategy_date(tmp_path) == date(2026, 11, 17)


def test_deleted_latest_file_is_not_returned_from_cache(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    latest = _partition(root, "2026-11-17")
    monkeypatch.setattr(strategy_date, "cn_now", lambda: datetime(2026, 11, 17, 16, 0, tzinfo=CN_TZ))
    assert latest_strategy_date(tmp_path) == date(2026, 11, 17)
    (latest / "part.parquet").unlink()
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)


def test_cache_isolates_asset_and_root_and_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(strategy_date, "cn_now", lambda: datetime(2026, 11, 17, 16, 0, tzinfo=CN_TZ))
    _partition(tmp_path / "kline_daily_enriched", "2026-11-17")
    _partition(tmp_path / "kline_etf_enriched", "2026-11-16")
    other = tmp_path / "other"
    assert latest_strategy_date(tmp_path) == date(2026, 11, 17)
    assert latest_strategy_date(tmp_path, "etf") == date(2026, 11, 16)
    assert latest_strategy_date(other) is None
    _partition(other / "kline_daily_enriched", "2026-11-13")
    assert latest_strategy_date(other) == date(2026, 11, 13)


def test_explicit_now_reads_current_partitions_and_does_not_reuse_cutoff(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    incomplete = _partition(root, "2026-11-17", complete=False)
    monkeypatch.setattr(strategy_date, "cn_now", lambda: datetime(2026, 11, 17, 16, 0, tzinfo=CN_TZ))
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
    (incomplete / "part.parquet").write_bytes(b"ok")
    assert latest_strategy_date(tmp_path, now=datetime(2026, 11, 17, 15, 29, tzinfo=CN_TZ)) == date(2026, 11, 16)
    assert latest_strategy_date(tmp_path, now=datetime(2026, 11, 17, 15, 30, tzinfo=CN_TZ)) == date(2026, 11, 17)


def test_completed_file_in_existing_empty_partition_is_seen_after_ttl(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    folder = _partition(root, "2026-11-17", complete=False)
    clock = [100.0]
    monkeypatch.setattr(strategy_date.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(strategy_date, "cn_now", lambda: datetime(2026, 11, 17, 16, 0, tzinfo=CN_TZ))
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
    (folder / "part.parquet").write_bytes(b"ok")
    clock[0] += strategy_date._DATE_CACHE_TTL_S
    assert latest_strategy_date(tmp_path) == date(2026, 11, 17)


def test_date_input_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(strategy_date, "_DATE_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(strategy_date, "cn_now", lambda: datetime(2026, 11, 17, 16, 0, tzinfo=CN_TZ))
    for name in ("a", "b", "c"):
        root = tmp_path / name
        _partition(root / "kline_daily_enriched", "2026-11-17")
        assert latest_strategy_date(root) == date(2026, 11, 17)
    assert len(strategy_date._date_cache) == 2
    assert str((tmp_path / "a" / "kline_daily_enriched").resolve()) not in {
        key[0] for key in strategy_date._date_cache
    }


def test_preview_boundary_does_not_admit_formal_today(tmp_path, monkeypatch):
    root = tmp_path / "kline_daily_enriched"
    _partition(root, "2026-11-16")
    _partition(root, "2026-11-17")
    current = [datetime(2026, 11, 17, 14, 49, 59, tzinfo=CN_TZ)]
    monkeypatch.setattr(strategy_date, "cn_now", lambda: current[0])
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
    current[0] = datetime(2026, 11, 17, 14, 50, tzinfo=CN_TZ)
    assert latest_strategy_date(tmp_path) == date(2026, 11, 16)
