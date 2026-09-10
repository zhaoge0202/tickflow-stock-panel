"""扶摇流式日K同步: 大历史不得在内存中累积后再写入。"""
from __future__ import annotations

import os
from datetime import date, datetime

import polars as pl
import pytest

from app.services import kline_sync
from app.tickflow.repository import DataStore, KlineRepository


def _daily(symbol: str, day: date) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol], "date": [day], "open": [10.0], "high": [11.0],
        "low": [9.0], "close": [10.5], "volume": [100.0], "amount": [1050.0],
    })


class _StreamingProvider:
    def __init__(self, chunks, error: Exception | None = None):
        self.chunks = chunks
        self.error = error
        self.get_daily_called = False

    def iter_daily(self, *args, **kwargs):
        yield from self.chunks
        if self.error:
            raise self.error

    def get_daily(self, *args, **kwargs):
        self.get_daily_called = True
        raise AssertionError("streaming provider must not collect a full daily DataFrame")


@pytest.fixture
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _route_fuyao(monkeypatch, provider):
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    from app.data_providers import custom
    monkeypatch.setattr(
        custom,
        "provider_has_dataset",
        lambda name, dataset: name == "fuyao" and dataset == "daily",
    )
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)


def test_sync_persists_streamed_fuyao_chunks_only_after_fetch(monkeypatch, repo):
    provider = _StreamingProvider([
        _daily("000001.SZ", date(2020, 1, 2)),
        _daily("000002.SZ", date(2020, 1, 2)),
    ])
    _route_fuyao(monkeypatch, provider)

    written = kline_sync.sync_and_persist_daily_batch(
        ["000001.SZ", "000002.SZ"], repo, object(),
        start_date=datetime(2020, 1, 1), end_date=datetime(2020, 1, 2),
    )

    assert written == 2
    assert provider.get_daily_called is False
    stored = pl.read_parquet(
        repo.store.data_dir / "kline_daily" / "date=2020-01-02" / "part.parquet"
    )
    assert set(stored["symbol"].to_list()) == {"000001.SZ", "000002.SZ"}
    assert not (repo.store.data_dir / ".daily_sync_staging").exists()


def test_sync_discards_staging_when_streaming_fails(monkeypatch, repo):
    provider = _StreamingProvider(
        [_daily("000001.SZ", date(2020, 1, 2))], error=RuntimeError("network lost")
    )
    _route_fuyao(monkeypatch, provider)

    with pytest.raises(RuntimeError, match="network lost"):
        kline_sync.sync_and_persist_daily_batch(
            ["000001.SZ"], repo, object(),
            start_date=datetime(2020, 1, 1), end_date=datetime(2020, 1, 2),
        )

    assert not list((repo.store.data_dir / "kline_daily").glob("date=*"))
    assert not (repo.store.data_dir / ".daily_sync_staging").exists()


def test_sync_sweeps_only_stale_staging(monkeypatch, repo):
    staging = repo.store.data_dir / ".daily_sync_staging"
    stale = staging / "stale"
    fresh = staging / "fresh"
    stale.mkdir(parents=True)
    fresh.mkdir()
    os.utime(stale, (1, 1))
    provider = _StreamingProvider([])
    _route_fuyao(monkeypatch, provider)

    written = kline_sync.sync_and_persist_daily_batch(
        ["000001.SZ"], repo, object(),
        start_date=datetime(2020, 1, 1), end_date=datetime(2020, 1, 2),
    )

    assert written == 0
    assert not stale.exists()
    assert fresh.exists()
