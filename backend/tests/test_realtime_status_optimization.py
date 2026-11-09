from __future__ import annotations

import threading
import time

import polars as pl

from app.api import data as data_api
from app.services.data_status_cache import DataStatusCache
from app.services.quote_service import QuoteService


def test_data_status_returns_stale_snapshot_while_refresh_runs(monkeypatch):
    """已过期的旧统计仍要立即返回: 重算转后台, 不阻塞 /status。"""
    cache = DataStatusCache({**data_api._TABLE_TTLS, "storage": 60, "last_runs": float("inf")})
    monkeypatch.setattr(data_api, "_status_cache", cache)
    old = {"latest_date": "2026-09-20"}
    cache._values["daily"] = old
    cache._expires["daily"] = 0.0
    started = threading.Event()
    release = threading.Event()

    def fetch():
        started.set()
        release.wait(2.0)
        return {"latest_date": "2026-09-21"}

    try:
        t0 = time.perf_counter()
        result = cache.snapshot({"daily": fetch}, is_busy=lambda: False)["daily"]
        elapsed = time.perf_counter() - t0

        assert result == old
        assert elapsed < 0.5
        assert started.wait(1.0)
    finally:
        release.set()
        thread = cache._thread
        if thread is not None:
            thread.join(4)


def test_quote_service_full_rebuild_is_single_flight(monkeypatch):
    service = QuoteService()
    started = threading.Event()
    release = threading.Event()
    calls: list[dict] = []

    def fake_flush(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        release.wait(2.0)

    monkeypatch.setattr(service, "_flush_live_enriched", fake_flush)
    daily = pl.DataFrame({"symbol": ["600000.SH"]})

    service._schedule_full_enriched_rebuild(
        daily,
        None,
        asset_type="stock",
        merge=False,
    )
    service._schedule_full_enriched_rebuild(
        daily,
        None,
        asset_type="stock",
        merge=False,
    )

    assert started.wait(1.0)
    assert len(calls) == 1
    release.set()
    for _ in range(20):
        if not service._enriched_rebuild_running:
            break
        time.sleep(0.01)
    assert not service._enriched_rebuild_running


def test_quote_service_does_not_sync_rebuild_on_missing_live_snapshot():
    class _Repo:
        def __init__(self):
            self.store = type("Store", (), {"data_dir": None})()
            self.warmups = 0

        def peek_live_agg(self):
            return pl.DataFrame()

        def peek_enriched_latest(self):
            return pl.DataFrame(), None

        def get_live_agg(self):
            raise AssertionError("hot path must not trigger synchronous live agg rebuild")

        def get_enriched_latest(self):
            raise AssertionError("hot path must not trigger synchronous enriched rebuild")

        def start_enriched_warmup(self):
            self.warmups += 1
            return True

    service = QuoteService()
    repo = _Repo()
    service._repo = repo
    service._flush_live_enriched(
        pl.DataFrame({"symbol": ["600000.SH"]}),
        asset_type="stock",
    )

    assert repo.warmups == 1
