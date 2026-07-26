"""指数监控规则校验测试。"""
import pytest

from app.strategy import monitor_rules


def _index_rule(rid="r_idx", **over):
    rule = {
        "id": rid, "name": rid, "type": "signal", "asset_type": "index",
        "scope": "symbols", "symbols": ["000001.SH"], "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 30}],
        "cooldown_seconds": 0, "enabled": True,
    }
    rule.update(over)
    return rule


def test_index_signal_price_allowed():
    monitor_rules.validate(_index_rule())
    monitor_rules.validate(_index_rule(type="price"))


def test_index_strategy_rejected():
    with pytest.raises(ValueError, match="指数"):
        monitor_rules.validate(_index_rule(type="strategy", strategy_id="s1"))


def test_index_market_rejected():
    with pytest.raises(ValueError, match="指数"):
        monitor_rules.validate(_index_rule(type="market"))


def test_index_scope_all_rejected():
    with pytest.raises(ValueError, match="指数"):
        monitor_rules.validate(_index_rule(scope="all", symbols=[]))


def test_index_intraday_signal_rejected():
    with pytest.raises(ValueError, match="分时"):
        monitor_rules.validate(_index_rule(
            conditions=[{"field": "signal_intraday_avg_cross_up", "op": "truth"}],
        ))


# ---- Task 7: B5 监控指数评估轮 ----

def _signal_rule(rid, asset_type, sym):
    return {
        "id": rid, "name": rid, "type": "signal", "asset_type": asset_type,
        "scope": "symbols", "symbols": [sym], "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0, "enabled": True,
    }


def test_evaluate_index_round_triggers_and_isolates():
    """指数轮只评估指数规则, 且不触碰策略结果缓存。"""
    import polars as pl
    from app.strategy.monitor import MonitorRuleEngine

    eng = MonitorRuleEngine()
    eng.set_rules([_signal_rule("r_idx", "index", "000001.SH"),
                   _signal_rule("r_stock", "stock", "000001.SH")])
    eng.set_name_map({"000001.SH": "上证指数"})
    df = pl.DataFrame({"symbol": ["000001.SH"], "close": [3000.0],
                       "change_pct": [0.01], "rsi_14": [40.0]})

    events = eng.evaluate(df, asset_type="index", reset_strategy_results=False)
    assert any(e["rule_id"] == "r_idx" for e in events)
    assert all(e["rule_id"] != "r_stock" for e in events)
    assert events[0]["name"] == "上证指数"
    assert eng.latest_strategy_results() == {}  # 策略结果缓存未被触碰
