"""策略辅助视图后台计算: 快速返回、请求合并与过期结果隔离。"""
from __future__ import annotations

import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import screener_work, strategy_run_queue


@pytest.fixture
def views(monkeypatch, tmp_path):
    from app.services import screener_views

    manager = strategy_run_queue.StrategyRunManager(queue_timeout_s=2)
    monkeypatch.setattr(strategy_run_queue, "MANAGER", manager)
    monkeypatch.setattr(screener_work, "hard_identity", lambda *args: ("identity",))
    monkeypatch.setattr(screener_work, "assert_current", lambda *args: None)
    version = ["v1"]
    monkeypatch.setattr(screener_work, "source_version", lambda *args: version[0])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return screener_views, repo, object(), manager, version


def _read(views, compute, **changes):
    service, repo, engine, _manager, _version = views
    return service.background_view(
        repo, engine, kind="focus", as_of=date(2026, 11, 17),
        asset_type="stock", timeframe="1d", parameters={"limit": 5, **changes},
        compute=compute, pending={"as_of": "2026-11-17", "versions": {}},
    )


def _wait_ready(views, compute, **changes):
    deadline = time.monotonic() + 2
    while True:
        result = _read(views, compute, **changes)
        if result["calculation"]["status"] != "pending":
            return result
        assert time.monotonic() < deadline
        threading.Event().wait(0.005)


def test_blocked_computation_returns_immediately_and_identical_requests_share_task(views):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def compute():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return {"as_of": "2026-11-17", "versions": {"final": {"total": 1}}}

    try:
        before = time.perf_counter()
        first = _read(views, compute)
        assert time.perf_counter() - before < 0.3
        assert first["calculation"]["status"] == "pending"
        assert entered.wait(1)
        second = _read(views, compute)
        assert second["calculation"] == first["calculation"]
        assert len(calls) == 1
        release.set()
        ready = _wait_ready(views, compute)
        assert ready["versions"]["final"]["total"] == 1
        assert ready["calculation"]["status"] == "ready"
        ready["versions"]["final"]["total"] = 9
        assert _read(views, compute)["versions"]["final"]["total"] == 1
    finally:
        release.set()


def test_parameters_and_data_versions_do_not_reuse_previous_results(views):
    calls = []

    def compute():
        calls.append(1)
        return {"as_of": "2026-11-17", "value": len(calls)}

    assert _wait_ready(views, compute)["value"] == 1
    assert _wait_ready(views, compute, limit=10)["value"] == 2
    views[-1][0] = "v2"
    assert _wait_ready(views, compute)["value"] == 3


def test_live_view_finishes_while_today_quotes_and_minutes_keep_updating(views, monkeypatch):
    """当日软更新不作废已捕获快照的任务, 硬历史版本仍参与身份。"""
    today_token = ["today-0"]
    history_token = ["history-0"]
    day = date(2026, 11, 17)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def version(_repo, _asset, timeframe, end):
        if timeframe == "1m" or end == day:
            return today_token[0]
        return history_token[0]

    monkeypatch.setattr(screener_work, "source_version", version)

    def compute():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"value": "captured-current"}

    first = _read(views, compute, live_snapshot=True)
    try:
        assert entered.wait(1)
        for i in range(4):
            today_token[0] = f"today-{i + 1}"
            assert _read(views, compute, live_snapshot=True)["calculation"]["task_id"] == first["calculation"]["task_id"]
        release.set()
        ready = _wait_ready(views, compute, live_snapshot=True)
        assert ready["value"] == "captured-current"
        assert calls == [1]
        history_token[0] = "history-fixed"
        updated = _read(views, compute, live_snapshot=True)
        assert updated["calculation"]["task_id"] != first["calculation"]["task_id"]
    finally:
        release.set()


def test_focus_preview_uses_frozen_live_context_before_daily_completion(monkeypatch, tmp_path):
    from datetime import datetime

    import polars as pl

    from app.market_time import CN_TZ
    from app.services import focus_versions

    target = date(2026, 11, 17)
    frame = pl.DataFrame({"symbol": ["A"], "date": [target], "close": [10.0]})
    context = SimpleNamespace(current=frame, history=frame)
    observed = []

    def build(*args, **kwargs):
        observed.append(kwargs)
        return context

    monkeypatch.setattr(focus_versions, "cn_now", lambda: datetime(2026, 11, 17, 14, 50, tzinfo=CN_TZ))
    monkeypatch.setattr(focus_versions, "ScreenerService", lambda *a, **k: SimpleNamespace(build_strategy_context=build))
    monkeypatch.setattr(focus_versions, "load_preview_snapshot", lambda *a: None)
    monkeypatch.setattr(focus_versions, "generate_preview_1450_from_context", lambda *a, **k: [{"symbol": "A"}])
    monkeypatch.setattr(focus_versions.strategy_config, "load_override", lambda *_: {})
    engine = SimpleNamespace(get=lambda _: SimpleNamespace(meta={"name": "示例"}))

    payload = focus_versions.build_focus_three_versions(
        SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), engine,
        strategy_id="demo", as_of=target, force_refresh=True, persist=False, strict=True,
    )

    assert observed[0]["live_snapshot"] is True
    assert payload["versions"]["preview"]["rows"][0]["symbol"] == "A"


def test_background_failure_is_an_error_and_does_not_become_empty_success(views):
    entered, release = threading.Event(), threading.Event()

    def compute():
        entered.set()
        assert release.wait(2)
        raise ValueError("unknown strategy: deleted")

    _read(views, compute)
    assert entered.wait(1)
    release.set()
    deadline = time.monotonic() + 2
    while True:
        try:
            _read(views, compute)
        except HTTPException as exc:
            assert exc.status_code == 404
            assert "unknown strategy" in exc.detail
            break
        assert time.monotonic() < deadline
        threading.Event().wait(0.005)


def test_generation_change_during_calculation_rejects_old_publication(views):
    entered, release = threading.Event(), threading.Event()

    def compute():
        entered.set()
        assert release.wait(2)
        return {"as_of": "2026-11-17", "value": "old"}

    first = _read(views, compute)
    try:
        assert entered.wait(1)
        views[-1][0] = "v2"
        release.set()
        deadline = time.monotonic() + 2
        while True:
            task = views[3].task_status(first["calculation"]["task_id"])
            if task and task["complete"]:
                assert task["error"]
                break
            assert time.monotonic() < deadline
            threading.Event().wait(0.005)
        ready = _wait_ready(views, lambda: {"value": "new"})
        assert ready["value"] == "new"
    finally:
        release.set()


def test_failure_remains_visible_after_frontend_poll_interval(views, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def compute():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        raise ValueError("invalid input")

    first = _read(views, compute)
    assert entered.wait(1)
    release.set()
    deadline = time.monotonic() + 2
    while not views[3].task_status(first["calculation"]["task_id"])["complete"]:
        assert time.monotonic() < deadline
        threading.Event().wait(0.005)
    original = time.time
    monkeypatch.setattr(strategy_run_queue.time, "time", lambda: original() + 2)
    with pytest.raises(HTTPException) as error:
        _read(views, compute)
    assert error.value.status_code == 400
    assert calls == [1]


def test_queue_full_does_not_wait_or_start_another_view(views, monkeypatch):
    from app.services.shared_work import WorkBusyError

    def reject(*args, **kwargs):
        raise WorkBusyError("队列已满")

    monkeypatch.setattr(views[3], "get_or_submit", reject)
    with pytest.raises(HTTPException) as error:
        _read(views, lambda: pytest.fail("拒绝后不能执行"))
    assert error.value.status_code == 503


def test_view_tasks_do_not_appear_in_minute_strategy_summaries(views):
    _wait_ready(views, lambda: {"value": 1})
    assert views[3].minute_summaries() == []


def test_preselect_endpoint_background_response_and_read_only_builder_flags(views, monkeypatch):
    from app.api import screener

    _service, repo, engine, _manager, _version = views
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))
    entered, release = threading.Event(), threading.Event()
    observed = []

    def build(*args, **kwargs):
        observed.append(kwargs)
        entered.set()
        assert release.wait(2)
        return {"mode": "preselect", "as_of": "2026-11-17", "results": {"demo": {"rows": [{"symbol": "A"}], "total": 1}}}

    monkeypatch.setattr(screener, "build_preselect_payload", build)
    monkeypatch.setattr(screener, "_admit_submission", lambda *_: None)
    monkeypatch.setattr(screener, "_load_ext_value_maps", lambda *_: {})
    req = screener.PreselectRequest(as_of=date(2026, 11, 17), strategy_ids=["demo"], background=True)
    try:
        pending = screener.preselect(req, request)
        assert pending["calculation"]["status"] == "pending"
        assert pending["results"] == {}
        assert entered.wait(1)
        assert observed[0]["persist"] is False and observed[0]["bypass_cache"] is True
        release.set()
        deadline = time.monotonic() + 2
        while True:
            ready = screener.preselect(req, request)
            if ready["calculation"]["status"] == "ready":
                assert ready["results"]["demo"]["total"] == 1
                break
            assert time.monotonic() < deadline
            threading.Event().wait(0.005)
    finally:
        release.set()


def test_focus_endpoint_background_keeps_pending_structure_and_does_not_persist(views, monkeypatch):
    from app.api import screener

    _service, repo, _engine, _manager, _version = views
    engine = SimpleNamespace(has=lambda _: True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))
    entered, release = threading.Event(), threading.Event()
    observed = []

    def build(*args, **kwargs):
        observed.append(kwargs)
        entered.set()
        assert release.wait(2)
        return {"as_of": "2026-11-17", "versions": {"final": {"total": 1, "rows": []}}}

    monkeypatch.setattr(screener, "build_focus_three_versions", build)
    monkeypatch.setattr(screener, "_admit_submission", lambda *_: None)
    try:
        payload = screener.focus_versions(request, "demo", date(2026, 11, 17), 5, None, True)
        assert payload["calculation"]["status"] == "pending"
        assert payload["current_stage"] == "waiting_preview"
        assert all(item["status"] == "pending" for item in payload["versions"].values())
        assert entered.wait(1)
        assert observed[0]["force_refresh"] is True and observed[0]["persist"] is False
    finally:
        release.set()


def test_focus_generation_does_not_write_full_or_preview_snapshots(tmp_path, monkeypatch):
    import json

    import polars as pl

    from app.market_time import CN_TZ
    from app.services import focus_versions
    from app.strategy.engine import StrategyDataContext, StrategyResult

    target = date(2026, 11, 16)
    full = tmp_path / "user_data" / "focus_three_versions_demo_2026-11-16.json"
    full.parent.mkdir()
    original = json.dumps({"as_of": str(target), "old": True})
    full.write_text(original)
    frame = pl.DataFrame({
        "symbol": ["A"], "date": [target], "open": [10.0], "close": [10.5],
        "high": [11.0], "low": [9.0], "volume": [100.0], "amount": [1000.0],
    })
    context = StrategyDataContext(asset_type="stock", timeframe="1d", as_of=target, current=frame, history=frame)
    runs = []

    def run(sid, context, **kwargs):
        runs.append(context)
        return StrategyResult(as_of=target, strategy_id=sid, rows=[{"symbol": "A", "close": 10.5}], total=1)

    engine = SimpleNamespace(get=lambda _: SimpleNamespace(meta={"name": "示例"}), run=run)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(focus_versions, "ScreenerService", lambda *a, **k: SimpleNamespace(build_strategy_context=lambda *a, **k: context))
    monkeypatch.setattr(focus_versions.strategy_config, "load_override", lambda *a: {})
    monkeypatch.setattr(focus_versions, "_preselect_rows", lambda *a, **k: [])
    from datetime import datetime

    monkeypatch.setattr(focus_versions, "cn_now", lambda: datetime(2026, 11, 17, 16, tzinfo=CN_TZ))

    payload = focus_versions.build_focus_three_versions(
        repo, engine, "demo", target, force_refresh=True, persist=False, strict=True,
    )

    assert payload["versions"]["final"]["total"] == 1
    assert len(runs) == 2  # 正式版与14:50版仍沿用原计算结果。
    assert full.read_text() == original
    assert not (full.parent / "preview_demo_2026-11-16.json").exists()


def test_strict_background_focus_does_not_turn_computation_error_into_no_match(tmp_path, monkeypatch):
    from app.services import focus_versions

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    engine = SimpleNamespace(get=lambda _: SimpleNamespace(meta={"name": "示例"}))

    def broken(*args, **kwargs):
        raise ValueError("history unavailable")

    monkeypatch.setattr(focus_versions, "ScreenerService", lambda *a, **k: SimpleNamespace(build_strategy_context=broken))
    monkeypatch.setattr(focus_versions.strategy_config, "load_override", lambda *a: {})
    with pytest.raises(ValueError, match="history unavailable"):
        focus_versions.build_focus_three_versions(
            repo, engine, "demo", date(2026, 11, 16), force_refresh=True, persist=False, strict=True,
        )


def test_preselect_background_bypasses_unversioned_cache_and_writes_no_history(tmp_path, monkeypatch):
    import polars as pl

    from app.services import auction_preselect, strategy_history
    from app.strategy.engine import StrategyDataContext

    target = date(2026, 11, 16)
    frame = pl.DataFrame({"symbol": ["A"], "date": [target]})
    context = StrategyDataContext(asset_type="stock", timeframe="1d", as_of=target, current=frame, history=frame)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    strategy = SimpleNamespace(meta={"name": "示例"})
    engine = SimpleNamespace(has=lambda _: True, get=lambda _: strategy, validate_context=lambda *a: None, resolve_params=lambda *a: {})
    monkeypatch.setattr(auction_preselect, "load_auction_config", lambda _: {"preselect_strategy_ids": ["demo"]})
    monkeypatch.setattr(auction_preselect, "ScreenerService", lambda *a, **k: SimpleNamespace(build_strategy_context=lambda *a, **k: context))
    monkeypatch.setattr(auction_preselect.strategy_config, "list_overrides", lambda *a: {})
    monkeypatch.setattr(auction_preselect, "_cache_get", lambda _: pytest.fail("后台任务不能消费未版本化缓存"))
    monkeypatch.setattr(auction_preselect, "_cache_set", lambda *a: pytest.fail("后台任务不能写未版本化缓存"))
    monkeypatch.setattr(strategy_history, "record_selection_snapshot", lambda *a, **k: pytest.fail("后台视图不写选择历史"))
    monkeypatch.setattr(auction_preselect, "_preselect_rows", lambda *a, **k: [{"symbol": "A"}])

    result = auction_preselect.build_preselect_payload(
        repo, engine, as_of=target, strategy_ids=["demo"], persist=False, bypass_cache=True,
    )

    assert result["results"]["demo"]["rows"] == [{"symbol": "A"}]


def test_focus_minute_partition_and_preview_snapshot_invalidate_completed_result(views, monkeypatch):
    _service, repo, _engine, _manager, _version = views
    minute_version = ["m1"]
    monkeypatch.setattr(screener_work, "source_version", lambda _repo, _asset, timeframe, _day: minute_version[0] if timeframe == "1m" else "d1")
    calls = []

    def compute():
        calls.append(1)
        return {"value": len(calls)}

    assert _wait_ready(views, compute, strategy_id="demo")["value"] == 1
    minute_version[0] = "m2"
    assert _wait_ready(views, compute, strategy_id="demo")["value"] == 2
    preview = repo.store.data_dir / "user_data" / "preview_demo_2026-11-17.json"
    preview.parent.mkdir()
    preview.write_text("{}")
    assert _wait_ready(views, compute, strategy_id="demo")["value"] == 3
