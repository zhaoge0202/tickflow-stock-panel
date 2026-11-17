"""独立策略调度与既有通知、模拟盘链路的接线回归。"""
import threading
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.services import quote_service
from app.services.quote_service import QuoteService
from app.strategy.monitor import MonitorRuleEngine

TODAY = date(2026, 11, 17)


def test_strategy_dispatch_does_not_wait_for_common_monitor(monkeypatch):
    service = QuoteService()
    stock = pl.DataFrame({"symbol": ["A"], "close": [10.0]})
    engine = MonitorRuleEngine()
    engine.set_rules([{"id": "focus", "type": "strategy", "strategy_id": "focus"}])
    service._repo = SimpleNamespace(get_name_map=lambda: {"A": "示例"})
    service._app_state = SimpleNamespace(monitor_engine=engine)
    service.get_enriched_today = lambda: (stock, TODAY)
    submissions = []
    service._strategy_workers = SimpleNamespace(
        submit=lambda df, asset_type: submissions.append((df, asset_type)),
    )
    monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY)
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: True)
    monkeypatch.setattr(service, "_monitor_worker_alive", lambda: True)
    # 普通监控尚未被唤醒, 策略也必须已经接到快照。
    service._monitor_wake = SimpleNamespace(set=lambda: submissions.append("common"))

    elapsed, asynchronous = service._dispatch_monitors(stock, None)

    assert elapsed == 0
    assert asynchronous is True
    assert len(submissions) == 2
    assert submissions[0][0] is stock
    assert submissions[0][1] == "stock"
    assert submissions[1] == "common"


def test_stale_asset_snapshot_is_not_submitted(monkeypatch):
    service = QuoteService()
    engine = MonitorRuleEngine()
    engine.set_rules([{"id": "etf", "type": "strategy", "asset_type": "etf"}])
    stale = pl.DataFrame({"symbol": ["A"]})
    service._repo = SimpleNamespace(
        get_name_map=lambda: {},
        get_enriched_latest_asset=lambda *a, **k: (stale, TODAY - timedelta(days=1)),
    )
    service._app_state = SimpleNamespace(monitor_engine=engine)
    service.get_enriched_today = lambda: (stale, TODAY - timedelta(days=1))
    submissions = []
    service._strategy_workers = SimpleNamespace(submit=lambda *a, **k: submissions.append(a))
    monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY)
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: True)

    service._submit_strategy_monitors()

    assert submissions == []


def test_common_monitor_excludes_strategies_when_workers_are_enabled(monkeypatch):
    service = QuoteService()
    service._strategy_workers = SimpleNamespace(active=True)
    frame = pl.DataFrame({"symbol": ["A"], "raw_close": [10.0]})
    service.get_enriched_today = lambda: (frame, TODAY)
    calls = []
    engine = SimpleNamespace(
        rule_count=1,
        set_name_map=lambda _: None,
        has_rule_type=lambda _: False,
        has_asset_rules=lambda _: False,
        intraday_signal_symbols=lambda _: set(),
        evaluate=lambda df, **kwargs: calls.append(kwargs) or [],
        consume_strategy_result_updates=lambda: False,
    )
    service._repo = SimpleNamespace(get_name_map=lambda: {})
    service._app_state = SimpleNamespace(monitor_engine=engine, repo=service._repo)
    monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY)
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: True)
    hooks = []
    monkeypatch.setattr(service, "_run_paper_hooks", lambda *a, **k: hooks.append(k))

    service._evaluate_monitors(frame, None)

    assert calls == [{"asset_type": "stock", "include_strategies": False}]
    assert hooks == [{"match_pending": True}]


def test_strategy_completion_publishes_results_without_repeating_paper_matching(monkeypatch):
    service = QuoteService()
    engine = SimpleNamespace(consume_strategy_result_updates=lambda: True)
    service._app_state = SimpleNamespace(monitor_engine=engine)
    subscriber = service.subscribe()
    hooks = []
    events = [{"rule_id": "focus"}]
    monkeypatch.setattr(service, "_deliver_monitor_events", lambda rows, _: rows)
    monkeypatch.setattr(service, "_run_paper_hooks", lambda df, feed, **kw: hooks.append((feed, kw)))

    service._on_strategy_monitor_events(events)

    assert subscriber.pop()["strategy_results_updated"]
    assert hooks == [(events, {"match_pending": False})]


def test_paper_watch_event_keeps_sandbox_delivery(monkeypatch):
    service = QuoteService()
    from app.strategy.paper_watch import PAPER_WATCH_RULE_PREFIX

    event = {"rule_id": f"{PAPER_WATCH_RULE_PREFIX}focus", "source": "strategy"}
    monkeypatch.setattr(service, "_refresh_alert_quotes", lambda *a: (_ for _ in ()).throw(AssertionError("沙盒不补价")))
    monkeypatch.setattr(service, "_broadcast_alerts", lambda *a: (_ for _ in ()).throw(AssertionError("沙盒不广播")))

    assert service._deliver_monitor_events([event], None) == [event]


def test_monitor_lifecycle_starts_strategy_workers_and_stops_them(monkeypatch):
    service = QuoteService()
    engine = MonitorRuleEngine()
    service.set_app_state(SimpleNamespace(monitor_engine=engine))
    evaluated = threading.Event()
    monkeypatch.setattr(service, "_evaluate_monitors", lambda *args: evaluated.set())
    try:
        service._start_monitor_worker()
        first = service._monitor_thread
        service._start_monitor_worker()
        assert service._monitor_thread is first
        assert service._strategy_workers.active
        service._monitor_wake.set()
        assert evaluated.wait(1)
        service._stop_monitor_worker()
        assert not service._strategy_workers.active
        assert not first.is_alive()
    finally:
        service._stop_monitor_worker()


def test_strategy_workers_initialize_when_monitor_engine_becomes_ready(monkeypatch):
    """启动时 boot_check 早于引擎装配, 下一轮仍须创建独立策略 worker。"""
    service = QuoteService()
    state = SimpleNamespace()
    service.set_app_state(state)
    service._running = True
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: False)
    monkeypatch.setattr(service, "_evaluate_monitors", lambda *args: None)
    try:
        service._start_monitor_worker()
        first = service._monitor_thread
        assert service._strategy_workers is None
        state.monitor_engine = MonitorRuleEngine()

        service._dispatch_monitors(pl.DataFrame(), None)

        assert service._strategy_workers is not None
        assert service._strategy_workers.active
        assert service._monitor_thread is first
    finally:
        service._running = False
        service._stop_monitor_worker()


def test_concurrent_dispatch_initializes_monitor_workers_once(monkeypatch):
    service = QuoteService()
    service._running = True
    service.set_app_state(SimpleNamespace(monitor_engine=MonitorRuleEngine()))
    entered, duplicate, release = threading.Event(), threading.Event(), threading.Event()
    alive = [False]
    calls = []

    def start():
        calls.append(threading.get_ident())
        if len(calls) > 1:
            duplicate.set()
        entered.set()
        assert release.wait(2)
        service._strategy_workers = object()
        alive[0] = True

    monkeypatch.setattr(service, "_start_monitor_worker", start)
    monkeypatch.setattr(service, "_monitor_worker_alive", lambda: alive[0])
    monkeypatch.setattr(service, "_submit_strategy_monitors", lambda: None)
    threads = [threading.Thread(target=service._dispatch_monitors, args=(pl.DataFrame(), None)) for _ in range(2)]
    try:
        threads[0].start()
        assert entered.wait(1)
        threads[1].start()
        assert not duplicate.wait(0.1)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(1)
    assert len(calls) == 1
    assert all(not thread.is_alive() for thread in threads)


def test_stopped_strategy_workers_do_not_hide_manual_strategy_evaluation(monkeypatch):
    service = QuoteService()
    service._strategy_workers = SimpleNamespace(active=False)
    frame = pl.DataFrame({"symbol": ["A"]})
    service.get_enriched_today = lambda: (frame, TODAY)
    calls = []
    engine = SimpleNamespace(
        rule_count=1, set_name_map=lambda _: None,
        has_rule_type=lambda _: False, has_asset_rules=lambda _: False,
        intraday_signal_symbols=lambda _: set(),
        evaluate=lambda df, **kwargs: calls.append(kwargs) or [],
        consume_strategy_result_updates=lambda: False,
    )
    service._repo = SimpleNamespace(get_name_map=lambda: {})
    service.set_app_state(SimpleNamespace(monitor_engine=engine, repo=service._repo))
    monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY)
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: True)
    monkeypatch.setattr(service, "_run_paper_hooks", lambda *a, **k: None)

    service._evaluate_monitors(frame, None)

    assert calls == [{"asset_type": "stock"}]


def test_status_exposes_independent_strategy_and_fetch_timings(monkeypatch):
    service = QuoteService()
    workers = [{"strategy_id": "focus", "asset_type": "stock", "running": True}]
    service._strategy_workers = SimpleNamespace(status=lambda: workers)
    service._last_fetch_stages_ms = {"network_ms": 100.0, "index_enriched_ms": 2.0}
    service._log_round_timing(0.3, 0, eval_async=True)
    monkeypatch.setattr(service, "realtime_mode", lambda: "full_market")
    monkeypatch.setattr(service, "_market_phase", lambda: "afternoon")
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: True)

    status = service.status()

    assert status["strategy_workers"] == workers
    assert status["fetch_stages_ms"] == {"network_ms": 100.0, "index_enriched_ms": 2.0}
    assert status["round_timing"] == {"fetch_ms": 300.0, "monitor_ms": 0.0, "monitor_async": True}
