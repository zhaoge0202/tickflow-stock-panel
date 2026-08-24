from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, PanelCache
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    get_enriched_generation,
)
from app.tickflow.repository import DataStore, KlineRepository


def _frame(value: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date(2026, 8, 14)],
        "open": [value],
        "high": [value],
        "low": [value],
        "close": [value],
        "volume": [1_000.0],
    })


def test_repository_enriched_noop_does_not_bump_generation(tmp_path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    frame = _frame()

    repo.append_enriched(frame)
    first = repo.get_matrix_data_generation("stock")
    repo.append_enriched(frame)

    assert repo.get_matrix_data_generation("stock") == first


def test_failed_multi_partition_publication_remains_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    publication = EnrichedPublication(tmp_path, recover=True)
    first = tmp_path / "kline_daily_enriched" / "date=2026-08-13" / "part.parquet"
    second = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    publication.write_parquet(_frame(10.0), first)

    original_write = pl.DataFrame.write_parquet

    def fail_second(self, path, *args, **kwargs):
        if "2026-08-14" in str(path):
            raise OSError("injected write failure")
        return original_write(self, path, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", fail_second)
    with pytest.raises(OSError, match="injected"):
        publication.write_parquet(_frame(11.0), second)

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "publishing"
    assert first.is_file()
    assert not second.is_file()
    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")


def test_recovery_replaces_stale_publication_but_not_active_owner(tmp_path) -> None:
    first = EnrichedPublication(tmp_path, recover=True)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    first.write_parquet(_frame(10.0), out)

    with pytest.raises(EnrichedGenerationUnavailableError, match="active"):
        EnrichedPublication(tmp_path, recover=True).write_parquet(_frame(11.0), out)

    del first
    recovered = EnrichedPublication(tmp_path, recover=True)
    recovered.write_parquet(_frame(12.0), out)
    recovered_generation = recovered.commit()

    assert recovered_generation == get_enriched_generation(tmp_path, "stock")
    assert pl.read_parquet(out)["close"].item() == pytest.approx(12.0)


def test_recovery_takes_over_when_owner_pid_is_dead_on_windows(tmp_path, monkeypatch) -> None:
    # 跨进程孤儿锁: 属主进程已死, 但 Windows 的 os.kill(pid, 0) 对不存在的 pid
    # 抛 WinError 87 (ERROR_INVALID_PARAMETER) 而非 ProcessLookupError,
    # 存活探测若把它当"存活", recover 将永远报 another publication is active。
    stale = {
        "state": "publishing",
        "generation": "stale-generation",
        "publication_id": "stale-publication",
        "owner_pid": 12345,
        "updated_at_ns": 0,
    }
    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps(stale), encoding="utf-8"
    )

    def probe(_pid: int, _sig: int) -> None:
        error = OSError()
        error.winerror = 87
        raise error

    monkeypatch.setattr("app.enriched_generation.os.kill", probe)

    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    recovered = EnrichedPublication(tmp_path, recover=True)
    recovered.write_parquet(_frame(10.0), out)
    recovered.commit()

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
    assert get_enriched_generation(tmp_path, "stock") == marker["generation"]
    assert pl.read_parquet(out)["close"].item() == pytest.approx(10.0)


def test_panel_cache_generation_change_forces_recompute() -> None:
    cache = PanelCache()
    calls: list[int] = []
    args = (["000001.SZ"], date(2026, 8, 13), date(2026, 8, 14), None)

    def compute(*_args):
        calls.append(len(calls) + 1)
        return pl.DataFrame({"value": [calls[-1]]})

    first = cache.get_or_compute(*args, compute, "stock", "generation-a")
    second = cache.get_or_compute(*args, compute, "stock", "generation-b")

    assert first["value"].item() == 1
    assert second["value"].item() == 2
    assert cache.stats()["compute_count"] == 2


def test_panel_reader_retries_when_generation_changes_during_scan(tmp_path) -> None:
    generations = iter(["generation-a", "generation-b", "generation-b", "generation-b"])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda _asset_type: next(generations),
    )
    engine = BacktestEngine(repo)
    calls: list[str] = []

    def load(*_args):
        calls.append("scan")
        return _frame(float(len(calls)))

    engine._load_panel_inner = load
    panel = engine.load_panel(
        None,
        date(2026, 8, 14),
        date(2026, 8, 14),
        columns=["symbol", "date", "close"],
    )

    assert calls == ["scan", "scan"]
    assert panel["close"].item() == pytest.approx(2.0)


def test_matrix_reader_retries_when_generation_changes_during_build(
    tmp_path,
    monkeypatch,
) -> None:
    generations = iter(["generation-a", "generation-b", "generation-b", "generation-b"])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda _asset_type: next(generations),
        get_instruments_asset=lambda _asset_type: pl.DataFrame(),
    )
    engine = BacktestEngine(repo)
    calls: list[str | None] = []
    expected = SimpleNamespace(
        execution_backend="matrix_native",
        base_columns={"open", "high", "low", "close", "volume"},
        instrument_columns=set(),
        matrix_columns=set(),
    )
    market = SimpleNamespace()

    def load_matrix(*_args, source_generation=None, **_kwargs):
        calls.append(source_generation)
        return market

    monkeypatch.setattr("app.backtest.engine.load_market_data_matrix_from_parquet", load_matrix)

    result = engine.load_market_data_matrix_for_backtest(
        None,
        date(2026, 8, 14),
        date(2026, 8, 14),
        expected,
    )

    assert result is market
    assert calls == ["generation-a", "generation-b"]


def test_live_flush_write_recovers_stale_marker_from_dead_process(
    tmp_path, monkeypatch
) -> None:
    """实时 enriched 落盘(repository 路径)遇到僵死 publishing 标记应接管自愈,
    而非持续抛错直到下一次盘后管道。"""
    from app.tickflow.repository import DataStore, KlineRepository

    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps({
            "state": "publishing",
            "generation": "stale-generation",
            "publication_id": "stale-publication",
            "owner_pid": 999999999,
            "updated_at_ns": 0,
        }),
        encoding="utf-8",
    )

    repo = KlineRepository(DataStore(tmp_path))
    repo.append_enriched(_frame(10.0))

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
