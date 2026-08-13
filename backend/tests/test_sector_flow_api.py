from __future__ import annotations

import threading
import time
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.api import sector_flow as sector_flow_api
from app.services import quote_tick_store
from app.services import sector_flow as sector_flow_service
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


def _write_multi_concept_members(tmp_path) -> None:
    config = ExtConfig(
        id="concept_flow_rank_test",
        label="概念排名测试",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("concept", "string", "所属概念"),
        ],
    )
    ExtConfigStore(tmp_path).upsert(config)
    ext_dir = tmp_path / "ext_data" / config.id
    pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "concept": ["强流入", "中流入", "弱流入"],
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
    assert payload["data_quality"]["status"] == "incomplete"
    assert payload["points"] == [_ms(9, 27), _ms(9, 28), _ms(9, 29), _ms(9, 30), _ms(9, 31)]
    assert sector["name"] == "芯片"
    assert sector["flow_source"] == "active_volume_estimate"
    assert sector["is_proxy"] is True
    assert sector["coverage_ratio"] == 1.0
    assert sector["flow_values"][:3] == [None, None, None]
    assert sector["flow_values"][3:] == pytest.approx([130_000, 126_000])
    assert payload["index"]["values"][:3] == [None, None, None]
    assert payload["index"]["values"][3:] == pytest.approx([1 / 30, 0.01])


def test_sector_flow_series_quality_ready_when_points_are_complete(tmp_path):
    _write_concept_members(tmp_path)
    rows = []
    for minute in range(27, 32):
        rows.extend([
            {
                "symbol": "A",
                "last_price": 10,
                "prev_close": 10,
                "active_net_volume": 100 + minute,
                "amount": 100_000,
                "timestamp": _ms(9, minute),
            },
            {
                "symbol": "B",
                "last_price": 20,
                "prev_close": 20,
                "active_net_volume": -50,
                "amount": 100_000,
                "timestamp": _ms(9, minute),
            },
            {
                "symbol": "000001.SH",
                "last_price": 3030,
                "prev_close": 3000,
                "timestamp": _ms(9, minute),
            },
        ])
    quote_tick_store.append_many(
        tmp_path,
        rows,
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

    assert payload["data_quality"]["status"] == "ready"
    assert payload["data_quality"]["expected_point_count"] == 5
    assert payload["data_quality"]["observed_point_count"] == 5
    assert payload["data_quality"]["point_coverage_ratio"] == pytest.approx(1.0)


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
    assert payload["points"] == [_ms(9, 27), _ms(9, 28), _ms(9, 29), _ms(9, 30)]
    assert sector["flow_values"][:3] == [None, None, None]
    assert sector["flow_values"][3:] == pytest.approx([30_000])
    assert payload["index"]["values"][:3] == [None, None, None]
    assert payload["index"]["values"][3:] == pytest.approx([0.01])


def test_sector_flow_series_fills_missing_minutes_between_ticks(tmp_path):
    _write_concept_members(tmp_path)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 100, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -50, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(9, 30)},
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 140, "amount": 140_000, "timestamp": _ms(9, 32)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -40, "amount": 120_000, "timestamp": _ms(9, 32)},
            {"symbol": "000001.SH", "last_price": 3060, "prev_close": 3000, "timestamp": _ms(9, 32)},
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
    assert payload["points"] == [_ms(9, 27), _ms(9, 28), _ms(9, 29), _ms(9, 30), _ms(9, 31), _ms(9, 32)]
    assert sector["flow_values"][:3] == [None, None, None]
    assert sector["flow_values"][3:] == pytest.approx([0, 0, 60_000])
    assert sector["strength_values"][3] is not None
    assert sector["strength_values"][4] is not None
    assert payload["index"]["values"][:3] == [None, None, None]
    assert payload["index"]["values"][3:] == pytest.approx([0.01, 0.01, 0.02])


def test_sector_flow_series_does_not_fill_lunch_break(tmp_path):
    _write_concept_members(tmp_path)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 100, "amount": 100_000, "timestamp": _ms(11, 30)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -50, "amount": 100_000, "timestamp": _ms(11, 30)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(11, 30)},
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 130, "amount": 130_000, "timestamp": _ms(13, 1)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -30, "amount": 120_000, "timestamp": _ms(13, 1)},
            {"symbol": "000001.SH", "last_price": 3060, "prev_close": 3000, "timestamp": _ms(13, 1)},
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

    points = payload["points"]
    assert _ms(11, 31) not in points
    assert _ms(12, 59) not in points
    assert _ms(13, 0) in points
    assert _ms(13, 1) in points

    sector = payload["sectors"][0]
    idx_1130 = points.index(_ms(11, 30))
    idx_1300 = points.index(_ms(13, 0))
    idx_1301 = points.index(_ms(13, 1))
    assert sector["flow_values"][idx_1130] == pytest.approx(0)
    assert sector["flow_values"][idx_1300] is None
    assert sector["strength_values"][idx_1300] is None
    assert sector["flow_values"][idx_1301] == pytest.approx(70_000)
    assert payload["index"]["values"][idx_1300] is None
    assert payload["index"]["values"][idx_1301] == pytest.approx(0.02)
    assert payload["data_quality"]["status"] == "incomplete"
    assert payload["data_quality"]["max_gap_seconds"] == 0


def test_sector_flow_series_carries_snapshot_until_next_afternoon_tick(tmp_path):
    _write_concept_members(tmp_path)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 100, "amount": 100_000, "timestamp": _ms(11, 30)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -50, "amount": 100_000, "timestamp": _ms(11, 30)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(11, 30)},
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 130, "amount": 130_000, "timestamp": _ms(14, 13)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -30, "amount": 120_000, "timestamp": _ms(14, 13)},
            {"symbol": "000001.SH", "last_price": 3060, "prev_close": 3000, "timestamp": _ms(14, 13)},
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

    points = payload["points"]
    sector = payload["sectors"][0]
    afternoon_gap = range(points.index(_ms(13, 0)), points.index(_ms(14, 13)))
    assert all(sector["flow_values"][idx] is None for idx in afternoon_gap)
    assert all(sector["strength_values"][idx] is None for idx in afternoon_gap)
    assert all(payload["index"]["values"][idx] is None for idx in afternoon_gap)
    assert sector["flow_values"][points.index(_ms(14, 13))] == pytest.approx(70_000)
    assert payload["index"]["values"][points.index(_ms(14, 13))] == pytest.approx(0.02)


def test_sector_flow_series_aligns_snapshots_by_ingest_time_without_future_leak(monkeypatch, tmp_path):
    _write_concept_members(tmp_path)

    monkeypatch.setattr(quote_tick_store.time, "time", lambda: _ms(13, 0) / 1000)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 100, "amount": 100_000, "timestamp": _ms(13, 0)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -50, "amount": 100_000, "timestamp": _ms(13, 0)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(13, 0)},
        ],
        source="tdxapi",
        force_flush=True,
    )

    monkeypatch.setattr(quote_tick_store.time, "time", lambda: _ms(14, 13) / 1000)
    quote_tick_store.append_many(
        tmp_path,
        [
            # TDX 个股时间可能停在上一笔成交，但本轮全市场快照是 14:13 抓到的。
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 130, "amount": 130_000, "timestamp": _ms(13, 0)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": -30, "amount": 120_000, "timestamp": _ms(13, 0)},
            {"symbol": "000001.SH", "last_price": 3060, "prev_close": 3000, "timestamp": _ms(13, 0)},
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

    points = payload["points"]
    sector = payload["sectors"][0]
    assert points[0] == _ms(13, 0)
    assert points[-1] == _ms(14, 13)
    assert sector["flow_values"][points.index(_ms(13, 0))] == pytest.approx(0)
    assert sector["flow_values"][points.index(_ms(13, 2))] == pytest.approx(0)
    assert sector["flow_values"][points.index(_ms(13, 3))] is None
    assert sector["flow_values"][points.index(_ms(14, 12))] is None
    assert sector["flow_values"][points.index(_ms(14, 13))] == pytest.approx(70_000)
    assert payload["index"]["values"][points.index(_ms(13, 0))] == pytest.approx(0.01)
    assert payload["index"]["values"][points.index(_ms(13, 2))] == pytest.approx(0.01)
    assert payload["index"]["values"][points.index(_ms(13, 3))] is None
    assert payload["index"]["values"][points.index(_ms(14, 12))] is None
    assert payload["index"]["values"][points.index(_ms(14, 13))] == pytest.approx(0.02)
    assert payload["data_quality"]["observed_point_count"] == 2
    assert payload["data_quality"]["point_coverage_ratio"] == pytest.approx(2 / len(points))
    assert payload["data_quality"]["max_gap_seconds"] > 60 * 60
    assert payload["data_quality"]["status"] == "incomplete"


def test_sector_flow_series_ranks_before_building_limited_curves(tmp_path):
    _write_multi_concept_members(tmp_path)
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "A", "last_price": 10, "prev_close": 10, "active_net_volume": 100, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "B", "last_price": 20, "prev_close": 20, "active_net_volume": 20, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "C", "last_price": 30, "prev_close": 30, "active_net_volume": 1, "amount": 100_000, "timestamp": _ms(9, 30)},
            {"symbol": "000001.SH", "last_price": 3030, "prev_close": 3000, "timestamp": _ms(9, 30)},
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
        limit=2,
    )

    assert [sector["name"] for sector in payload["sectors"]] == ["强流入", "中流入"]
    assert all(len(sector["flow_values"]) == len(payload["points"]) for sector in payload["sectors"])


def test_sector_flow_series_coalesces_concurrent_requests(monkeypatch, tmp_path):
    _write_concept_members(tmp_path)
    original = sector_flow_service._build_sector_flow_series
    calls = 0
    calls_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def slow_build(**kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait(timeout=2)
        return original(**kwargs)

    monkeypatch.setattr(sector_flow_service, "_build_sector_flow_series", slow_build)
    request = _request(tmp_path)
    results = []

    def load_series() -> None:
        results.append(sector_flow_api.sector_flow_series(
            request,
            kind="concept",
            metric="main_flow",
            trade_date=TRADE_DATE,
            step_seconds=60,
            limit=5,
        ))

    first = threading.Thread(target=load_series)
    second = threading.Thread(target=load_series)
    first.start()
    assert started.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert calls == 1
    assert len(results) == 2
