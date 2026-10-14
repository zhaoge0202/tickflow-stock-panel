"""财务数据 custom 源选择测试。"""
from __future__ import annotations

import polars as pl

from app.services import financial_sync as fs
from app.tickflow.capabilities import CapabilitySet


def test_effective_financial_provider_falls_back_to_selected_tdxapi(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda name, dataset: name == "tdxapi" and dataset == "financial",
    )

    assert fs._effective_custom_financial_provider() == "tdxapi"
    assert fs._financial_is_custom()


def test_sync_table_uses_effective_custom_financial_provider(monkeypatch, tmp_path):
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    class FakeProvider:
        def __init__(self) -> None:
            self.calls = []

        def get_financials(self, table, symbols, latest_only=True):
            self.calls.append((table, symbols, latest_only))
            return pl.DataFrame([
                {"symbol": "600519.SH", "source": "tdxapi", "net_profit": 1.0},
            ])

    fake_provider = FakeProvider()

    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda name, dataset: name == "tdxapi" and dataset == "financial",
    )
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: fake_provider)

    rows = fs._sync_table(
        "metrics",
        ["600519.SH"],
        tmp_path,
        CapabilitySet(),
        latest_only=True,
    )

    assert rows == 1
    assert fake_provider.calls == [("metrics", ["600519.SH"], True)]
    out = pl.read_parquet(tmp_path / "financials" / "metrics" / "part.parquet")
    assert out["symbol"][0] == "600519.SH"
    assert out["source"][0] == "tdxapi"
