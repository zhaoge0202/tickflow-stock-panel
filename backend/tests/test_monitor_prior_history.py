"""监控暖机历史与当日快照分开后, 分钟策略的日线输入仍要含今天。"""
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.strategy import monitor
from app.strategy.engine import StrategyResult


def test_minute_monitor_daily_window_contains_latest_current_bar(monkeypatch):
    today = date(2026, 11, 17)
    historical = pl.DataFrame({
        "symbol": ["A"] * 5,
        "date": [today - timedelta(days=i) for i in range(5, 0, -1)],
        "close": [1.0, 2.0, 3.0, 4.0, 5.0],
    })
    current = pl.DataFrame({"symbol": ["A"], "date": [today], "close": [11.0]})
    observed = []
    definition = SimpleNamespace(
        meta={"id": "minute", "name": "分钟示例"}, execution_backend="minute_filter",
        minute_daily_bars=3, filter_history_fn=None,
    )

    def run(_sid, context, **_kwargs):
        observed.append(context.daily_history)
        return StrategyResult(strategy_id="minute", as_of=today, rows=[], total=0)

    engine = monitor.MonitorRuleEngine()
    engine.set_strategy_engine(SimpleNamespace(get=lambda _: definition, run=run))
    engine.set_history_loader(lambda *_: historical)
    engine.set_minute_loader(lambda *_: pl.DataFrame())
    engine.set_rules([{"id": "minute-rule", "type": "strategy", "strategy_id": "minute", "scope": "all"}])
    monkeypatch.setattr(monitor, "cn_today", lambda: today)

    engine.evaluate(current)

    assert len(observed) == 1
    daily = observed[0]
    assert daily["date"].to_list() == [today - timedelta(days=i) for i in range(3, -1, -1)]
    assert daily.filter(pl.col("date") == today)["close"].to_list() == [11.0]
    assert daily.select("symbol", "date").n_unique() == daily.height
