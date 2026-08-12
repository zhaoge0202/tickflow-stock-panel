from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.api import sector_flow as sector_flow_api
from app.services import quote_tick_store
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.services.sector_monitor import SectorMonitorService

CN = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 8)


class _Repo:
    def __init__(self, data_dir):
        self.store = SimpleNamespace(data_dir=data_dir)

    def get_index_instruments(self) -> pl.DataFrame:
        return pl.DataFrame()


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 7, 8, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def _request(tmp_path):
    repo = _Repo(tmp_path)
    state = SimpleNamespace(
        repo=repo,
        sector_monitor_service=SectorMonitorService(repo),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _write_concept_members(tmp_path) -> None:
    config = ExtConfig(
        id="concept_flow_test",
        label="概念测试",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("concept", "string", "所属概念"),
        ],
    )
    ExtConfigStore(tmp_path).upsert(config)
    ext_dir = tmp_path / "ext_data" / config.id
    pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E"],
        "concept": ["芯片", "芯片", "芯片", "芯片", "芯片"],
    }).write_parquet(ext_dir / "part.parquet")


def test_sector_flow_series_aggregates_active_net_amount(tmp_path):
    _write_concept_members(tmp_path)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 100, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -50, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "C", "last_price": 30, "prev_close": 30, "active_net_volume": 10, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "D", "last_price": 40, "prev_close": 40, "active_net_volume": 0, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "E", "last_price": 50, "prev_close": 50, "active_net_volume": 20, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "000001.SH", "last_price": 3100, "prev_close": 3000, "timestamp": _ms(9, 30)},
            {"symbol": "A", "last_price": 11, "prev_close": 10, "active_net_volume": 150, "amount": 130_000, "timestamp": _ms(9, 31)},
            {"symbol": "B", "last_price": 19, "prev_close": 20, "active_net_volume": -30, "amount": 120_000, "timestamp": _ms(9, 31)},
            {"symbol": "C", "last_price": 31, "prev_close": 30, "active_net_volume": 15, "amount": 130_000, "timestamp": _ms(9, 31)},
            {"symbol": "D", "last_price": 41, "prev_close": 40, "active_net_volume": 5, "amount": 120_000, "timestamp": _ms(9, 31)},
            {"symbol": "E", "last_price": 49, "prev_close": 50, "active_net_volume": -10, "amount": 110_000, "timestamp": _ms(9, 31)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(9, 31)},
        ],
        source="tdxapi",
        force_flush=True,
    )

    payload = sector_flow_api.sector_flow_series(
        _request(tmp_path),
        kind="concept",
        metric="main_flow",
        trade_date=TRADE_DATE,
        step_seconds=60,
        limit=5,
    )

    sector = payload["sectors"][0]
    assert payload["data_quality"]["status"] == "ready"
    assert payload["points"] == [_ms(9, 30), _ms(9, 31)]
    assert sector["name"] == "芯片"
    assert sector["flow_source"] == "active_volume_estimate"
    assert sector["is_proxy"] is True
    assert sector["coverage_ratio"] == 1.0
    assert sector["flow_values"][0] == pytest.approx(130_000)
    assert sector["flow_values"][1] == pytest.approx(126_000)
    assert payload["index"]["values"] == pytest.approx([1 / 30, 0.01])


def test_sector_flow_series_returns_empty_status_without_ticks(tmp_path):
    _write_concept_members(tmp_path)

    payload = sector_flow_api.sector_flow_series(
        _request(tmp_path),
        kind="concept",
        metric="main_flow",
        trade_date=TRADE_DATE,
        step_seconds=60,
        limit=5,
    )

    assert payload["data_quality"]["status"] == "empty"
    assert payload["sectors"] == []
    assert payload["points"] == []


def test_sector_flow_series_uses_last_tick_in_each_time_bucket(tmp_path):
    _write_concept_members(tmp_path)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 10, "amount": 100_000, "timestamp": _ms(9, 30, 10)},
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 20, "amount": 100_000, "timestamp": _ms(9, 30, 50)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": 5, "amount": 100_000, "timestamp": _ms(9, 30, 20)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(9, 30, 40)},
        ],
        source="tdxapi",
        force_flush=True,
    )

    payload = sector_flow_api.sector_flow_series(
        _request(tmp_path),
        kind="concept",
        metric="main_flow",
        trade_date=TRADE_DATE,
        step_seconds=60,
        limit=5,
    )

    sector = payload["sectors"][0]
    assert payload["points"] == [_ms(9, 30)]
    assert sector["flow_values"] == pytest.approx([30_000])
    assert payload["index"]["values"] == pytest.approx([0.01])
