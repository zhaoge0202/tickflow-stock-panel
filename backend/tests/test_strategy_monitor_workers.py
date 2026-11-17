"""实时策略按组并发、快照合并与失效边界。"""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import cn_today
from app.strategy.engine import StrategyResult
from app.strategy.monitor import MonitorRuleEngine


def _quotes(value: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A"], "date": [cn_today()], "close": [value],
        "change_pct": [0.01], "quote_ts": [int(time.time() * 1000)],
    })


def _rule(sid: str, **changes) -> dict:
    return {
        "id": f"rule_{sid}", "name": sid, "type": "strategy",
        "asset_type": "stock", "strategy_id": sid, "scope": "all",
        "symbols": [], "cooldown_seconds": 0,
        "notify_events": ["buy_signal", "pool_entry"], **changes,
    }


class _Strategies:
    definition_revision = 0

    def __init__(self, hook=None, *, matrix=False):
        self.hook = hook or (lambda *_: None)
        self.matrix = matrix
        self.calls: dict[str, list[float]] = {}
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.keys: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.lock = threading.Lock()

    def get(self, sid):
        return SimpleNamespace(
            meta={"id": sid, "name": sid}, filter_history_fn=None,
            execution_backend="matrix_native" if self.matrix else "polars_expr",
        )

    def required_history_bars(self, *args, **kwargs):
        return 1

    def prepare_realtime_matrix(self, context, strategy_ids, **kwargs):
        with self.lock:
            self.keys.append((strategy_ids[0], context.cache_key))
        return object()

    def release_realtime_matrix(self, key):
        with self.lock:
            self.released.append(key)

    def run(self, sid, context, **kwargs):
        value = context.current["close"][0]
        with self.lock:
            self.calls.setdefault(sid, []).append(value)
            self.active[sid] = self.active.get(sid, 0) + 1
            self.max_active[sid] = max(self.max_active.get(sid, 0), self.active[sid])
        try:
            self.hook(sid, value)
            return StrategyResult(
                strategy_id=sid, as_of=cn_today(), total=1,
                rows=[{"symbol": "A", "close": value, "change_pct": 0.01}],
                entry_signal_hits=[{"symbol": "A", "signals": ["buy"]}],
            )
        finally:
            with self.lock:
                self.active[sid] -= 1


def _wait_until(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "等待并发结果超时"
        threading.Event().wait(0.005)


def _workers(engine, callback):
    from app.services.strategy_monitor_workers import StrategyMonitorWorkers

    workers = StrategyMonitorWorkers(engine, callback)
    workers.start()
    return workers


def test_light_rules_do_not_run_strategies_or_clear_their_results():
    strategies = _Strategies()
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_rules([_rule("focus"), {
        "id": "price", "type": "price", "asset_type": "stock", "scope": "all",
        "conditions": [{"field": "close", "op": ">", "value": 5.0}],
        "cooldown_seconds": 0,
    }])
    monitor.evaluate(_quotes())
    before = monitor.latest_strategy_results()
    monitor.consume_strategy_result_updates()

    events = monitor.evaluate(_quotes(), include_strategies=False)

    assert strategies.calls == {"focus": [10.0]}
    assert monitor.latest_strategy_results() == before
    assert not monitor.consume_strategy_result_updates()
    assert [event["rule_id"] for event in events] == ["price"]


def test_blocked_apex_does_not_delay_focus_or_overwrite_its_results():
    entered, release = threading.Event(), threading.Event()

    def hook(sid, _value):
        if sid == "apex":
            entered.set()
            assert release.wait(3)

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(_Strategies(hook))
    monitor.set_rules([_rule("apex"), _rule("focus")])
    callbacks = []
    workers = _workers(monitor, lambda events: callbacks.append(monitor.latest_strategy_results()))
    try:
        workers.submit(_quotes())
        assert entered.wait(1)
        _wait_until(lambda: "focus" in monitor.latest_strategy_results())
        assert "apex" not in monitor.latest_strategy_results()
        assert callbacks and "focus" in callbacks[0]
        release.set()
        _wait_until(lambda: len(monitor.latest_strategy_results()) == 2)
        assert set(monitor.latest_strategy_results()) == {"focus", "apex"}
    finally:
        release.set()
        workers.stop()


def test_busy_strategy_only_keeps_latest_pending_snapshot_and_never_reenters():
    entered, release = threading.Event(), threading.Event()

    def hook(_sid, value):
        if value == 1:
            entered.set()
            assert release.wait(3)

    strategies = _Strategies(hook)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_rules([_rule("focus")])
    workers = _workers(monitor, lambda _events: None)
    try:
        workers.submit(_quotes(1))
        assert entered.wait(1)
        for value in (2, 3, 4):
            workers.submit(_quotes(value))
        release.set()
        _wait_until(lambda: strategies.calls.get("focus") == [1, 4])
        _wait_until(lambda: monitor.latest_strategy_results()["focus"]["rows"][0]["close"] == 4)
        assert strategies.max_active == {"focus": 1}
    finally:
        release.set()
        workers.stop()


@pytest.mark.parametrize("change", ["delete", "edit", "strategy", "daily", "revision"])
def test_inflight_result_is_discarded_after_configuration_or_data_change(change, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    data_revision = ["0"]

    def hook(_sid, _value):
        entered.set()
        assert release.wait(3)

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(_Strategies(hook))
    monitor.set_data_dir(tmp_path)
    monkeypatch.setattr("app.services.daily_health.revision", lambda *_: data_revision[0])
    monitor.set_rules([_rule("focus")])
    callbacks = []
    workers = _workers(monitor, callbacks.append)
    try:
        workers.submit(_quotes())
        assert entered.wait(1)
        if change == "delete":
            monitor.set_rules([])
        elif change == "edit":
            monitor.set_rules([_rule("focus", symbols=["B"], scope="symbols")])
        elif change == "strategy":
            monitor.invalidate_strategy_state()
        elif change == "daily":
            monitor.invalidate_daily_asset("stock")
        else:
            data_revision[0] = "1"
        release.set()
        _wait_until(lambda: not any(item["running"] for item in workers.status()))
        assert monitor.latest_strategy_results() == {}
        assert callbacks == []
    finally:
        release.set()
        workers.stop()


def test_rule_removal_clears_published_result_and_same_config_reload_keeps_baseline():
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(_Strategies())
    rule = _rule("focus")
    monitor.set_rules([rule])
    callbacks = []
    workers = _workers(monitor, callbacks.append)
    try:
        workers.submit(_quotes())
        _wait_until(lambda: len(callbacks) == 1)
        monitor.set_rules([dict(rule)])
        workers.submit(_quotes(11))
        _wait_until(lambda: len(callbacks) == 2)
        assert callbacks == [[], []]
        monitor.remove_rule(rule["id"])
        assert monitor.latest_strategy_results() == {}
    finally:
        workers.stop()


def test_stop_restart_cannot_overlap_same_strategy_or_publish_old_result():
    entered, release = threading.Event(), threading.Event()

    def hook(_sid, value):
        if value == 1:
            entered.set()
            assert release.wait(3)

    strategies = _Strategies(hook)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_rules([_rule("focus")])
    callbacks = []
    workers = _workers(monitor, callbacks.append)
    try:
        assert workers.active
        workers.submit(_quotes(1))
        assert entered.wait(1)
        workers.stop()
        assert not workers.active
        assert not callbacks
        workers.start()
        assert workers.active
        workers.submit(_quotes(2))
        assert strategies.calls["focus"] == [1]
        release.set()
        _wait_until(lambda: len(callbacks) == 1)
        assert monitor.latest_strategy_results()["focus"]["rows"][0]["close"] == 2
        assert strategies.max_active["focus"] == 1
    finally:
        release.set()
        workers.stop()


def test_matrix_workers_use_distinct_mutable_buffer_keys_and_idle_input_creates_no_threads():
    strategies = _Strategies(matrix=True)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_history_loader(lambda *_: pl.DataFrame())
    workers = _workers(monitor, lambda _events: None)
    try:
        workers.submit(_quotes())
        assert workers.status() == []
        monitor.set_rules([_rule("focus"), _rule("apex")])
        workers.submit(pl.DataFrame())
        assert workers.status() == []
        workers.submit(_quotes())
        _wait_until(lambda: len(monitor.latest_strategy_results()) == 2)
        keys = dict(strategies.keys)
        assert set(keys) == {"focus", "apex"}
        assert keys["focus"] != keys["apex"]
        assert all(key != "monitor:stock" for key in keys.values())
    finally:
        workers.stop()


def test_failed_strategy_preserves_last_valid_result_and_other_strategy_progress():
    def hook(sid, value):
        if sid == "apex" and value == 11:
            raise ValueError("样例策略失败")

    strategies = _Strategies(hook)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_rules([_rule("apex"), _rule("focus")])
    callbacks = []
    workers = _workers(monitor, callbacks.append)
    try:
        workers.submit(_quotes())
        _wait_until(lambda: len(callbacks) == 2)
        monitor.consume_strategy_result_updates()
        workers.submit(_quotes(11))
        _wait_until(lambda: strategies.calls.get("apex") == [10, 11])
        _wait_until(lambda: len(callbacks) == 3)
        _wait_until(lambda: not any(item["running"] for item in workers.status()))
        results = monitor.latest_strategy_results()
        assert results["apex"]["rows"][0]["close"] == 10
        assert results["focus"]["rows"][0]["close"] == 11
        assert len(callbacks) == 3
    finally:
        workers.stop()


def test_daily_invalidation_keeps_signal_baseline_and_deduplication():
    class _SignalStrategies(_Strategies):
        def run(self, sid, context, **kwargs):
            result = super().run(sid, context, **kwargs)
            if context.current["close"][0] == 10:
                result.entry_signal_hits = []
            return result

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(_SignalStrategies())
    monitor.set_rules([_rule("focus")])
    callbacks = []
    workers = _workers(monitor, callbacks.append)
    try:
        workers.submit(_quotes())
        _wait_until(lambda: len(callbacks) == 1)
        monitor.invalidate_daily_asset("stock")
        workers.submit(_quotes(11))
        _wait_until(lambda: len(callbacks) == 2)
        assert [(event["type"], event["symbol"]) for event in callbacks[1]] == [("buy_signal", "A")]
        workers.submit(_quotes(11))
        _wait_until(lambda: len(callbacks) == 3)
        assert callbacks[2] == []
    finally:
        workers.stop()


def test_stop_from_callback_does_not_deadlock_and_releases_matrix_cache():
    strategies = _Strategies(matrix=True)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_history_loader(lambda *_: pl.DataFrame())
    monitor.set_rules([_rule("focus")])
    delivered = threading.Event()
    workers = None

    def callback(_events):
        workers.stop()
        delivered.set()

    workers = _workers(monitor, callback)
    workers.start()  # 重复 start 不得启动第二组线程。
    workers.submit(_quotes())
    try:
        assert delivered.wait(1)
        _wait_until(lambda: bool(strategies.released))
        assert len(strategies.keys) == 1
        assert strategies.released == [strategies.keys[0][1]]
        assert workers.status() == []
    finally:
        workers.stop()


def test_date_cleanup_cannot_race_strategy_deduplication_commit(monkeypatch):
    entered, release_strategy = threading.Event(), threading.Event()
    cleaning, release_cleanup = threading.Event(), threading.Event()
    committing, delivered = threading.Event(), threading.Event()

    def hook(_sid, _value):
        entered.set()
        assert release_strategy.wait(3)

    class _PausedItems(dict):
        def items(self):
            cleaning.set()
            assert release_cleanup.wait(3)
            return super().items()

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(_Strategies(hook))
    monitor.set_rules([_rule("focus")])
    strategy_key = ("rule_focus", "A", "buy_signal")
    old_date_key = ("date", "_date_2000-01-01", "date")
    monitor._last_fire = {strategy_key: 100.0, old_date_key: 100.0}
    original_commit = monitor.commit_strategy_worker

    def commit(*args):
        committing.set()
        return original_commit(*args)

    monkeypatch.setattr(monitor, "commit_strategy_worker", commit)
    workers = _workers(monitor, lambda _events: delivered.set())
    date_thread = threading.Thread(target=monitor.evaluate_date_rules)
    try:
        workers.submit(_quotes())
        assert entered.wait(1)
        monitor._last_fire = _PausedItems(monitor._last_fire)
        date_thread.start()
        assert cleaning.wait(1)
        release_strategy.set()
        assert committing.wait(1)
        assert not delivered.wait(0.05)
        release_cleanup.set()
        assert delivered.wait(1)
        date_thread.join(1)
        assert not date_thread.is_alive()
        assert strategy_key in monitor._last_fire
        assert old_date_key not in monitor._last_fire
    finally:
        release_strategy.set()
        release_cleanup.set()
        workers.stop()
        if date_thread.ident is not None:
            date_thread.join(1)


def test_real_matrix_buffer_update_cannot_change_other_strategy_snapshot(monkeypatch):
    """使用真实可变矩阵, 防止只读 view 被另一策略的下一轮原地覆写。"""
    import numpy as np

    from app.strategy.engine import StrategyEngine

    today = cn_today()
    history = pl.DataFrame([
        {
            "symbol": "A", "date": today - timedelta(days=ago),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
            "volume": 10000.0, "amount": 100000.0,
        }
        for ago in range(70, 0, -1)
    ])
    strategies = StrategyEngine(
        strategy_dirs=[Path(__file__).parent / "fixtures" / "strategies"],
    )
    shared_definition = strategies.get("macd_golden")
    monkeypatch.setattr(strategies, "get", lambda _sid: shared_definition)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategies)
    monitor.set_history_loader(lambda *_: history)
    monitor.set_rules([_rule("focus"), _rule("apex")])
    entered, release, focus_twice = threading.Event(), threading.Event(), threading.Event()
    slow_views, slow_values = [], []
    focus_runs = []

    def run(sid, context, **kwargs):
        if sid == "apex":
            slow_views.append(context.market.close)
            entered.set()
            assert release.wait(3)
            slow_values.append(float(context.market.close[-1, 0]))
        else:
            focus_runs.append(context.market.close)
            if len(focus_runs) == 2:
                focus_twice.set()
        return StrategyResult(strategy_id=sid, as_of=today, rows=[], total=0)

    monkeypatch.setattr(strategies, "run", run)
    workers = _workers(monitor, lambda _events: None)
    try:
        workers.submit(_quotes(10))
        assert entered.wait(1)
        _wait_until(lambda: "focus" in monitor.latest_strategy_results())
        workers.submit(_quotes(11))
        assert focus_twice.wait(2)
        assert not np.shares_memory(slow_views[0], focus_runs[-1])
        assert slow_views[0][-1, 0] == 10
        release.set()
        _wait_until(lambda: len(slow_values) >= 1)
        assert slow_values[0] == 10
    finally:
        release.set()
        workers.stop()


def test_worker_state_copy_is_safe_while_price_rule_creates_cooldown(monkeypatch):
    copying, resume, price_entered = threading.Event(), threading.Event(), threading.Event()

    class _PausedItems(dict):
        def items(self):
            iterator = iter(super().items())
            yield next(iterator)
            copying.set()
            assert resume.wait(3)
            yield from iterator

    monitor = MonitorRuleEngine()
    rule = _rule("focus")
    monitor.set_rules([rule, {
        "id": "price", "type": "price", "asset_type": "stock", "scope": "all",
        "conditions": [{"field": "close", "op": ">", "value": 5}],
        "cooldown_seconds": 30,
    }])
    monitor._last_fire = _PausedItems({("existing", "A", "price"): 0})
    errors, children, price_events = [], [], []

    def copy_worker():
        try:
            children.append(monitor.create_strategy_worker([rule], "isolated"))
        except RuntimeError as exc:
            errors.append(exc)

    original_rule = monitor._evaluate_rule

    def evaluate_rule(*args):
        price_entered.set()
        return original_rule(*args)

    monkeypatch.setattr(monitor, "_evaluate_rule", evaluate_rule)
    factory = threading.Thread(target=copy_worker)
    prices = threading.Thread(target=lambda: price_events.extend(
        monitor.evaluate(_quotes(), include_strategies=False),
    ))
    try:
        factory.start()
        assert copying.wait(1)
        prices.start()
        assert price_entered.wait(1)
        prices.join(0.05)
        assert prices.is_alive(), "复制去重状态期间, 普通规则不能并发新增字典键"
    finally:
        resume.set()
        factory.join(1)
        if prices.ident is not None:
            prices.join(1)
    assert not factory.is_alive() and not prices.is_alive()
    assert errors == []
    assert len(children) == 1
    assert [event["rule_id"] for event in price_events] == ["price"]
    assert ("price", "A", "price") in monitor._last_fire
