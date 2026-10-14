from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import polars as pl

from app.data_providers import custom as custom_sources
from app.services import index_sync
from app.tickflow.capabilities import CapabilitySet


class _Repo:
    def __init__(self, tmp_path) -> None:
        self.store = SimpleNamespace(data_dir=tmp_path)
        self.raw = pl.DataFrame()
        self.enriched = pl.DataFrame()
        self.refreshed = 0

    def append_etf_daily(self, df: pl.DataFrame) -> None:
        self.raw = df

    def append_etf_enriched(self, df: pl.DataFrame) -> None:
        self.enriched = df

    def refresh_index_views(self) -> None:
        self.refreshed += 1


class _Provider:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls = []

    def get_daily(self, symbols, start_time, end_time, asset_type, on_chunk_done=None):
        self.calls.append({
            "symbols": symbols,
            "start_time": start_time,
            "end_time": end_time,
            "asset_type": asset_type,
        })
        if on_chunk_done:
            on_chunk_done(1, 1)
        return self.frame


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["510300.SH", "510300.SH", "510300.SH", "BAD.SH"],
        "date": [
            datetime(2024, 1, 2).date(),
            datetime(2024, 1, 2).date(),
            datetime(2024, 1, 3).date(),
            datetime(2024, 1, 2).date(),
        ],
        "open": [3.0, 3.1, 0.0, 1.0],
        "high": [3.1, 3.2, 3.2, 1.1],
        "low": [2.9, 3.0, 3.0, 0.9],
        "close": [3.0, 3.1, 3.1, 1.0],
        "volume": [100.0, 110.0, 120.0, 10.0],
        "amount": [300.0, 341.0, 372.0, 10.0],
    })


def test_tdx_provider_sync_does_not_require_tickflow_batch(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)
    provider = _Provider(_frame())
    progress = []
    monkeypatch.setattr(index_sync.preferences, "get_daily_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(index_sync, "compute_enriched", lambda raw, **kwargs: raw)

    written = index_sync.sync_and_persist_etf_daily(
        repo,
        CapabilitySet(),
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 31),
        symbols_override=["510300.SH"],
        on_chunk_done=lambda current, total: progress.append((current, total)),
    )

    assert written == 1
    assert provider.calls[0]["asset_type"] == "etf"
    assert provider.calls[0]["symbols"] == ["510300.SH"]
    assert repo.raw.select("symbol", "date", "close").to_dicts() == [{
        "symbol": "510300.SH",
        "date": datetime(2024, 1, 2).date(),
        "close": 3.1,
    }]
    assert repo.enriched.equals(repo.raw)
    assert repo.refreshed == 1
    assert progress == [(1, 1)]


def test_tickflow_without_batch_capability_still_returns_zero(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)
    monkeypatch.setattr(index_sync.preferences, "get_daily_data_provider", lambda: "tickflow")

    written = index_sync.sync_and_persist_etf_daily(
        repo,
        CapabilitySet(),
        symbols_override=["510300.SH"],
    )

    assert written == 0
    assert repo.raw.is_empty()
    assert repo.refreshed == 0
