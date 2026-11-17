"""历史缓存命中不得等待其他日期重算, 清理和数据修正不能复活旧结果。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import screener as screener_module
from app.services.screener import ScreenerService
from app.services.shared_work import WorkBusyError

TARGET = date(2026, 11, 17)


@pytest.fixture(autouse=True)
def isolated_cache():
    ScreenerService.clear_history_cache()
    yield
    ScreenerService.clear_history_cache()


def service(root, *, generation=None):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=root),
        get_enriched_history=lambda *_: None,
        get_instruments_asset=lambda *_: pl.DataFrame({"symbol": ["A"]}),
    )
    if generation is not None:
        repo.get_enriched_generation_snapshot = lambda *_: SimpleNamespace(through=lambda _: generation[0])
    return ScreenerService(repo)


def patch_source(monkeypatch, compute):
    frame = pl.DataFrame({
        "symbol": ["A"] * 10,
        "date": [TARGET - timedelta(days=i) for i in range(9, -1, -1)],
        "close": [float(i) for i in range(10)],
    })
    monkeypatch.setattr(screener_module, "scan_enriched_parquet", lambda *_: frame.lazy())
    monkeypatch.setattr(ScreenerService, "_compute_enriched_batches", compute)


def test_cache_hit_does_not_wait_for_another_date_computation(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()

    def compute(_self, source, _symbols, *, result_start, result_end):
        if result_end != TARGET:
            entered.set()
            assert release.wait(3)
        return source.collect().filter(pl.col("date") <= result_end)

    patch_source(monkeypatch, compute)
    svc = service(tmp_path)
    expected = svc._load_enriched_history(TARGET, 3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cold = pool.submit(svc._load_enriched_history, TARGET - timedelta(days=1), 3)
        try:
            assert entered.wait(1)
            hit = pool.submit(svc._load_enriched_history, TARGET, 3)
            actual = hit.result(timeout=0.25)
            assert actual.equals(expected)
        finally:
            release.set()
            cold.result(timeout=2)


def test_cache_isolated_by_data_root(monkeypatch, tmp_path):
    calls = []

    def compute(self, source, _symbols, **_kwargs):
        calls.append(self.repo.store.data_dir)
        return source.collect().with_columns(pl.lit(float(len(calls))).alias("close"))

    patch_source(monkeypatch, compute)
    first = service(tmp_path / "first")._load_enriched_history(TARGET, 3)
    second = service(tmp_path / "second")._load_enriched_history(TARGET, 3)

    assert len(calls) == 2
    assert first["close"][0] == 1
    assert second["close"][0] == 2


def test_same_missing_window_computes_once_and_different_misses_stay_serial(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls, running, maximum = [], [0], [0]

    def compute(_self, source, _symbols, *, result_start, result_end):
        with lock:
            calls.append(result_end)
            running[0] += 1
            maximum[0] = max(maximum[0], running[0])
        try:
            entered.set()
            assert release.wait(3)
            return source.collect().filter(pl.col("date") <= result_end)
        finally:
            with lock:
                running[0] -= 1

    patch_source(monkeypatch, compute)
    svc = service(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(svc._load_enriched_history, TARGET, 3)
        try:
            assert entered.wait(1)
            same = pool.submit(svc._load_enriched_history, TARGET, 3)
            other = pool.submit(svc._load_enriched_history, TARGET - timedelta(days=1), 3)
            assert not same.done()
        finally:
            release.set()
        assert first.result(timeout=2).equals(same.result(timeout=2))
        other.result(timeout=2)
    assert calls.count(TARGET) == 1
    assert maximum == [1]


def test_clear_does_not_wait_for_compute_and_rejects_old_completion(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def compute(_self, source, _symbols, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return source.collect().with_columns(pl.lit(float(len(calls))).alias("close"))

    patch_source(monkeypatch, compute)
    svc = service(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(svc._load_enriched_history, TARGET, 3)
        try:
            assert entered.wait(1)
            cleared = pool.submit(ScreenerService.clear_history_cache)
            cleared.result(timeout=0.25)
        finally:
            release.set()
        with pytest.raises(WorkBusyError):
            old.result(timeout=2)
    assert svc._load_enriched_history(TARGET, 3)["close"][0] == 2


@pytest.mark.parametrize("change", ["generation", "daily_revision"])
def test_data_version_change_recomputes_and_inflight_old_result_is_rejected(monkeypatch, tmp_path, change):
    entered, release = threading.Event(), threading.Event()
    generation, revision, calls = ["g0"], ["r0"], []
    monkeypatch.setattr("app.services.daily_health.revision", lambda *_: revision[0])

    def compute(_self, source, _symbols, **_kwargs):
        calls.append(1)
        if len(calls) == 2:
            entered.set()
            assert release.wait(3)
        return source.collect().with_columns(pl.lit(float(len(calls))).alias("close"))

    patch_source(monkeypatch, compute)
    svc = service(tmp_path, generation=generation)
    assert svc._load_enriched_history(TARGET, 3)["close"][0] == 1
    current = generation if change == "generation" else revision
    current[0] = "updated"
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(svc._load_enriched_history, TARGET, 3)
        try:
            assert entered.wait(1)
            current[0] = "updated-again"
        finally:
            release.set()
        with pytest.raises(WorkBusyError):
            old.result(timeout=2)
    assert svc._load_enriched_history(TARGET, 3)["close"][0] == 3


def test_cache_memory_budget_evicts_old_dates_and_does_not_retain_oversized_frame(monkeypatch, tmp_path):
    calls = []

    def compute(_self, source, _symbols, *, result_start, result_end):
        calls.append(result_end)
        return source.collect().filter(pl.col("date") <= result_end)

    patch_source(monkeypatch, compute)
    monkeypatch.setattr(pl.DataFrame, "estimated_size", lambda *_a, **_k: 100)
    monkeypatch.setattr(screener_module, "_HISTORY_CACHE_MAX_BYTES", 150)
    svc = service(tmp_path)
    svc._load_enriched_history(TARGET, 3)
    svc._load_enriched_history(TARGET - timedelta(days=1), 3)
    assert len(screener_module._history_cache) == 1
    svc._load_enriched_history(TARGET, 3)
    assert calls.count(TARGET) == 2
    monkeypatch.setattr(screener_module, "_HISTORY_CACHE_MAX_BYTES", 99)
    svc._load_enriched_history(TARGET - timedelta(days=1), 3)
    assert screener_module._history_cache == {}


def test_clearing_one_asset_preserves_other_asset_cached_reads(monkeypatch, tmp_path):
    calls = []

    def compute(self, source, _symbols, **_kwargs):
        calls.append(self.asset_type)
        return source.collect()

    patch_source(monkeypatch, compute)
    stock = service(tmp_path)
    etf = ScreenerService(stock.repo, asset_type="etf")
    stock._load_enriched_history(TARGET, 3)
    etf._load_enriched_history(TARGET, 3)
    ScreenerService.clear_history_cache("stock")
    etf._load_enriched_history(TARGET, 3)
    stock._load_enriched_history(TARGET, 3)
    assert calls == ["stock", "etf", "stock"]


def test_failed_owner_unblocks_shared_waiter_and_allows_retry(monkeypatch, tmp_path):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def compute(_self, source, _symbols, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            raise ValueError("历史计算失败样例")
        return source.collect()

    original = screener_module.Future.result

    def result(future, *args, **kwargs):
        waiting.set()
        return original(future, *args, **kwargs)

    patch_source(monkeypatch, compute)
    svc = service(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(svc._load_enriched_history, TARGET, 3)
        try:
            assert entered.wait(1)
            monkeypatch.setattr(screener_module.Future, "result", result)
            second = pool.submit(svc._load_enriched_history, TARGET, 3)
            assert waiting.wait(1)
        finally:
            release.set()
        with pytest.raises(ValueError, match="历史计算失败样例"):
            first.result(timeout=2)
        with pytest.raises(ValueError, match="历史计算失败样例"):
            second.result(timeout=2)
    assert len(calls) == 1
    assert not screener_module._history_pending
    assert not svc._load_enriched_history(TARGET, 3).is_empty()
    assert len(calls) == 2


def test_monitor_history_excludes_today_and_ignores_today_only_generation(monkeypatch, tmp_path):
    from app.enriched_generation import EnrichedGenerationSnapshot

    generation = [EnrichedGenerationSnapshot("g0", "base")]
    revision, calls = ["r0"], []
    monkeypatch.setattr("app.services.daily_health.revision", lambda *_: revision[0])

    def compute(_self, source, _symbols, *, result_start, result_end):
        calls.append(result_end)
        return source.collect().filter(pl.col("date") <= result_end)

    patch_source(monkeypatch, compute)
    svc = service(tmp_path)
    svc.repo.get_enriched_generation_snapshot = lambda *_: generation[0]
    first = svc.load_monitor_history(TARGET, 3)
    assert first["date"].max() < TARGET
    generation[0] = generation[0].changed("today-write", TARGET)
    assert svc.load_monitor_history(TARGET, 3).equals(first)
    assert len(calls) == 1
    revision[0] = "repair"
    svc.load_monitor_history(TARGET, 3)
    assert len(calls) == 2
    generation[0] = generation[0].changed("history-write", TARGET - timedelta(days=1))
    svc.load_monitor_history(TARGET, 3)
    assert len(calls) == 3


def test_repository_cache_keeps_requested_window_not_fallback_floor(monkeypatch, tmp_path):
    calls = []
    frame = pl.DataFrame({"symbol": ["A"], "date": [TARGET], "close": [10.0]})
    svc = service(tmp_path)

    def repository_history(_target, lookback):
        calls.append(lookback)
        return frame if lookback <= 3 else None

    svc.repo.get_enriched_history = repository_history
    monkeypatch.setattr(screener_module, "scan_enriched_parquet", lambda *_: pytest.fail("应命中repo缓存"))
    assert svc._load_enriched_history(TARGET, 3).equals(frame)
    assert calls == [3]
    assert screener_module._history_cache == {}


@pytest.mark.parametrize("change", ["clear", "generation", "daily_revision"])
def test_cached_crop_rejects_clear_or_source_change_before_return(monkeypatch, tmp_path, change):
    entered, release = threading.Event(), threading.Event()
    generation, revision = ["g0"], ["r0"]
    monkeypatch.setattr("app.services.daily_health.revision", lambda *_: revision[0])
    patch_source(monkeypatch, lambda _self, source, _symbols, **_kwargs: source.collect())
    svc = service(tmp_path, generation=generation)
    svc._load_enriched_history(TARGET, 3)
    original_crop = ScreenerService._crop_history_cache

    def crop(frame, lookback):
        entered.set()
        assert release.wait(3)
        return original_crop(frame, lookback)

    monkeypatch.setattr(ScreenerService, "_crop_history_cache", staticmethod(crop))
    with ThreadPoolExecutor(max_workers=1) as pool:
        hit = pool.submit(svc._load_enriched_history, TARGET, 3)
        try:
            assert entered.wait(1)
            if change == "clear":
                ScreenerService.clear_history_cache()
            elif change == "generation":
                generation[0] = "g1"
            else:
                revision[0] = "r1"
        finally:
            release.set()
        with pytest.raises(WorkBusyError):
            hit.result(timeout=2)


@pytest.mark.parametrize("explicit_current", [True, False])
def test_live_context_uses_captured_current_while_today_generation_keeps_changing(monkeypatch, tmp_path, explicit_current):
    from app.enriched_generation import EnrichedGenerationSnapshot

    generation = [EnrichedGenerationSnapshot("g0", "historical")]
    svc = service(tmp_path)
    svc.repo.get_enriched_generation_snapshot = lambda *_: generation[0]
    # live 装配不得把不同实时帧套入同一个共享矩阵缓存身份。
    svc.repo.get_matrix_data_generation = lambda *_: pytest.fail("live context 不取当日共享矩阵代次")
    current = pl.DataFrame({"symbol": ["A"], "date": [TARGET], "close": [123.0]})
    older_current = current.with_columns(pl.lit(999.0).alias("close"))
    loads = []

    def load_current(_target):
        loads.append(_target)
        generation[0] = generation[0].changed("capture-write", TARGET)
        return current

    def compute(_self, source, _symbols, *, result_start, result_end):
        generation[0] = generation[0].changed("during-history-write", TARGET)
        return pl.concat([source.collect(), older_current], how="diagonal_relaxed").filter(
            pl.col("date") <= result_end,
        )

    monkeypatch.setattr(svc, "_load_enriched_for_date", load_current)
    patch_source(monkeypatch, compute)
    engine = SimpleNamespace(required_history_bars=lambda *_a, **_k: 3)
    context = svc.build_strategy_context(
        engine, TARGET, ["demo"], live_snapshot=True,
        **({"current": current} if explicit_current else {}),
    )

    assert context.current is current
    assert context.data_generation is None
    assert context.history["date"].unique().sort().to_list() == [
        TARGET - timedelta(days=offset) for offset in range(3, -1, -1)
    ]
    assert context.history.filter(pl.col("date") == TARGET)["close"].to_list() == [123.0]
    assert loads == ([] if explicit_current else [TARGET])


@pytest.mark.parametrize("change", ["clear", "historical_generation", "daily_revision"])
def test_live_context_rejects_historical_change_during_snapshot_assembly(monkeypatch, tmp_path, change):
    from app.enriched_generation import EnrichedGenerationSnapshot

    generation, revision = [EnrichedGenerationSnapshot("g0", "historical")], ["r0"]
    monkeypatch.setattr("app.services.daily_health.revision", lambda *_: revision[0])
    svc = service(tmp_path)
    svc.repo.get_enriched_generation_snapshot = lambda *_: generation[0]
    history = pl.DataFrame({"symbol": ["A"], "date": [TARGET - timedelta(days=1)], "close": [1.0]})
    current = pl.DataFrame({"symbol": ["A"], "date": [TARGET], "close": [10.0]})

    def load_monitor(_target, _bars):
        if change == "historical_generation":
            generation[0] = generation[0].changed("repair", TARGET - timedelta(days=1))
        elif change == "daily_revision":
            revision[0] = "r1"
        else:
            ScreenerService.clear_history_cache()
        return history

    monkeypatch.setattr(svc, "load_monitor_history", load_monitor)
    engine = SimpleNamespace(required_history_bars=lambda *_a, **_k: 3)
    with pytest.raises(WorkBusyError):
        svc.build_strategy_context(engine, TARGET, ["demo"], current=current, live_snapshot=True)


def test_live_snapshot_option_does_not_change_minute_context(monkeypatch, tmp_path):
    svc = service(tmp_path)
    current = pl.DataFrame({"symbol": ["A"], "date": [TARGET], "close": [10.0]})
    minute = pl.DataFrame({"symbol": ["A"], "close": [11.0]})
    monkeypatch.setattr(svc, "_history_cache_identity", lambda *_: pytest.fail("分钟路径不加载日线实时身份"))
    monkeypatch.setattr(svc, "_load_minute_history", lambda *_: minute)
    engine = SimpleNamespace(minute_daily_history_bars=lambda *_: 0)
    context = svc.build_strategy_context(engine, TARGET, ["minute"], current=current, timeframe="1m", live_snapshot=True)
    assert context.current is current
    assert context.history is minute
    assert context.daily_history is None
