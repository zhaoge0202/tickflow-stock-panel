from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import data as data_api
from app.backtest.engine import PanelCache
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    get_enriched_generation,
)


class _RepoStub:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)
        self.calls: list[str] = []

    def clear_cache(self) -> None:
        self.calls.append("clear_cache")

    def refresh_cache(self) -> None:
        self.calls.append("refresh_cache")

    def rebuild_views(self) -> None:
        self.calls.append("rebuild_views")


def _request(repo: _RepoStub) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _write_parquet_placeholder(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"parquet-placeholder")


def _stub_clear_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import overview
    from app.services import alert_store
    from app.services.pipeline_jobs import job_store
    from app.services.screener import ScreenerService

    monkeypatch.setattr(job_store, "clear", lambda: None)
    monkeypatch.setattr(alert_store, "clear", lambda _data_dir: None)
    monkeypatch.setattr(ScreenerService, "clear_history_cache", lambda: None)
    monkeypatch.setattr(overview, "invalidate_overview_cache", lambda: None)
    monkeypatch.setattr(data_api, "invalidate_data_cache", lambda _table=None: None)


def test_clear_data_bumps_enriched_generations_and_invalidates_panel_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_clear_side_effects(monkeypatch)
    repo = _RepoStub(tmp_path)
    stock_file = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    etf_file = tmp_path / "kline_etf_enriched" / "date=2026-08-14" / "part.parquet"
    _write_parquet_placeholder(stock_file)
    _write_parquet_placeholder(etf_file)
    stock_before = get_enriched_generation(tmp_path, "stock")
    etf_before = get_enriched_generation(tmp_path, "etf")

    cache = PanelCache()
    cache_args = (["000001.SZ"], date(2026, 8, 14), date(2026, 8, 14), None)
    computes: list[int] = []

    def compute(*_args) -> pl.DataFrame:
        computes.append(len(computes) + 1)
        return pl.DataFrame({"value": [computes[-1]]})

    cache.get_or_compute(*cache_args, compute, "stock", stock_before)
    result = data_api.clear_data(_request(repo))
    stock_after = get_enriched_generation(tmp_path, "stock")
    etf_after = get_enriched_generation(tmp_path, "etf")
    cached_after = cache.get_or_compute(*cache_args, compute, "stock", stock_after)

    assert result == {"deleted_files": 2}
    assert not stock_file.exists()
    assert not etf_file.exists()
    assert stock_after != stock_before
    assert etf_after != etf_before
    assert cached_after["value"].item() == 2
    assert cache.stats()["compute_count"] == 2
    assert repo.calls == ["clear_cache", "refresh_cache", "rebuild_views"]


def test_clear_data_restores_ready_generation_when_first_delete_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _RepoStub(tmp_path)
    target = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    _write_parquet_placeholder(target)
    generation_before = get_enriched_generation(tmp_path, "stock")
    original_unlink = Path.unlink

    def fail_target(path: Path, *args, **kwargs) -> None:
        if path == target:
            raise PermissionError("injected delete failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)

    with pytest.raises(PermissionError, match="injected delete failure"):
        data_api.clear_data(_request(repo))

    assert target.is_file()
    assert get_enriched_generation(tmp_path, "stock") == generation_before
    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"


def test_clear_data_keeps_generation_publishing_after_partial_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _RepoStub(tmp_path)
    enriched_dir = tmp_path / "kline_daily_enriched"
    first = enriched_dir / "date=2026-08-13" / "part.parquet"
    second = enriched_dir / "date=2026-08-14" / "part.parquet"
    _write_parquet_placeholder(first)
    _write_parquet_placeholder(second)
    get_enriched_generation(tmp_path, "stock")
    original_unlink = Path.unlink
    parquet_unlinks = 0

    def fail_second_parquet(path: Path, *args, **kwargs) -> None:
        nonlocal parquet_unlinks
        if path.suffix == ".parquet" and enriched_dir in path.parents:
            parquet_unlinks += 1
            if parquet_unlinks == 2:
                raise PermissionError("injected partial delete failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second_parquet)

    with pytest.raises(PermissionError, match="injected partial delete failure"):
        data_api.clear_data(_request(repo))

    assert sum(path.exists() for path in (first, second)) == 1
    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "publishing"
    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")


def test_clear_data_recovers_stale_publishing_marker_from_dead_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部进程(崩掉的脚本/中断的管道)残留 publishing 标记且 owner pid 已死时,
    清空不应 500 — clear 语义就是接管一切 (用户实测场景: POST /api/data/clear
    报 another enriched publication is incomplete)。"""
    _stub_clear_side_effects(monkeypatch)
    repo = _RepoStub(tmp_path)
    target = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    _write_parquet_placeholder(target)
    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps({
            "state": "publishing",
            "generation": "stale-generation",
            "publication_id": "stale-publication",
            "owner_pid": 999999999,  # 不存在的 pid → 探测判定已死
            "updated_at_ns": 0,
        }),
        encoding="utf-8",
    )

    result = data_api.clear_data(_request(repo))

    assert result == {"deleted_files": 1}
    assert not target.exists()
    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
    assert get_enriched_generation(tmp_path, "stock") == marker["generation"]
