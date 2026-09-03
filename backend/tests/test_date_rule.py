"""日期提醒 (date) 规则测试: 窗口判定 + 引擎按天去重 + 校验/归一化。

date 规则是纯日历、无行情条件, 由 quote_service 盘中轮询调用
MonitorRuleEngine.evaluate_date_rules()。这里只测引擎与规则存储层。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine, format_alert_quote


def _rule(**kw):
    r = {
        "id": "date1", "name": "批次到期", "type": "date",
        "asset_type": "stock", "scope": "symbols",
        "symbols": ["600519.SH"],
        "remind_date": "2026-08-30", "lead_days": 3,
        "cooldown_seconds": 86400, "severity": "info", "enabled": True,
        "message": "批次到期提醒 · 2026-08-30",
    }
    r.update(kw)
    return r


# ── 窗口纯函数 ──────────────────────────────────────────
def test_window_pure_unit_cases():
    w = monitor_rules.date_rule_in_window
    assert w("2026-08-30", 3, "2026-08-28") is True   # 窗口左界内
    assert w("2026-08-30", 3, "2026-08-27") is True   # 恰在左界 (含)
    assert w("2026-08-30", 3, "2026-08-26") is False  # 早于左界
    assert w("2026-08-30", 3, "2026-08-30") is True   # 到期当天
    assert w("2026-08-30", 3, "2026-08-31") is False  # 过期
    assert w("2026-08-30", 0, "2026-08-30") is True   # 不提前
    assert w("2026-08-30", 0, "2026-08-29") is False
    assert w("not-a-date", 3, "2026-08-28") is False   # 非法输入 fail-safe
    assert w("2026-08-30", "x", "2026-08-28") is False


def test_validate_and_normalize_date():
    valid = {"id": "date2", "name": "n", "type": "date", "scope": "symbols",
             "symbols": ["600519.SH"], "remind_date": "2026-09-01", "lead_days": 2}
    monitor_rules.validate(valid)
    n = monitor_rules.normalize(valid)
    assert n["cooldown_seconds"] == 86400   # 每天最多一次
    assert n["conditions"] == []
    assert n["scope"] == "symbols"

    # 归一化后为空 symbols 仍合法 (手动建), 但校验拒绝空 symbols (锚定标的)
    with pytest.raises(ValueError):
        monitor_rules.validate({**valid, "symbols": []})

    with pytest.raises(ValueError):
        monitor_rules.validate({**valid, "remind_date": None})          # 必填
    with pytest.raises(ValueError):
        monitor_rules.validate({**valid, "remind_date": "2026/09/01"})  # 格式
    with pytest.raises(ValueError):
        monitor_rules.validate({**valid, "lead_days": -1})              # 非负
    with pytest.raises(ValueError):
        monitor_rules.validate({**valid, "conditions": [{"field": "close", "op": ">=", "value": 10.0}]})
    assert "date" in monitor_rules.RULE_TYPES


# ── 引擎评估 ────────────────────────────────────────────
def _monkey_today(monkeypatch, iso: str):
    from datetime import date

    monkeypatch.setattr("app.strategy.monitor.cn_today", lambda: date.fromisoformat(iso))


def test_date_engine_fires_in_window(monkeypatch):
    _monkey_today(monkeypatch, "2026-08-28")
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    evs = eng.evaluate_date_rules()
    assert len(evs) == 1
    ev = evs[0]
    assert ev["source"] == "date"
    assert ev["type"] == "date_reminder"
    assert ev["symbol"] == "600519.SH"
    assert "2天后到期" in ev["message"]  # 2026-08-30 - 今天(08-28)
    assert ev["message"]  # 单标的: 不再把 symbol 拼进 message
    assert "600519.SH" not in ev["message"]
    assert ev["price"] is None and ev["change_pct"] is None  # 日期提醒无行情
    assert ev["conditions"] == [] and ev["logic"] == "and"


def test_date_engine_today_expiry_wording(monkeypatch):
    _monkey_today(monkeypatch, "2026-08-30")
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(lead_days=0)])
    evs = eng.evaluate_date_rules()
    assert len(evs) == 1
    assert "今日到期" in evs[0]["message"]


def test_date_engine_skips_outside_window(monkeypatch):
    _monkey_today(monkeypatch, "2026-08-31")  # 过期
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    assert eng.evaluate_date_rules() == []


def test_date_engine_ignores_disabled_and_non_date(monkeypatch):
    _monkey_today(monkeypatch, "2026-08-28")
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(enabled=False), _rule(id="price1", type="price", conditions=[
        {"field": "close", "op": ">=", "value": 100.0},
    ], message="")])
    assert eng.evaluate_date_rules() == []


def test_date_engine_once_per_day(monkeypatch):
    _monkey_today(monkeypatch, "2026-08-28")
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    assert len(eng.evaluate_date_rules()) == 1
    assert eng.evaluate_date_rules() == []          # 同日 cooldown 去重
    # 跨天: 窗口内仍命中 → 再提醒一次
    _monkey_today(monkeypatch, "2026-08-29")
    assert len(eng.evaluate_date_rules()) == 1


def test_date_engine_alerts_handler_called(monkeypatch):
    _monkey_today(monkeypatch, "2026-08-28")
    seen: list[dict] = []
    eng = MonitorRuleEngine(alert_handler=seen.append)
    eng.set_rules([_rule()])
    eng.evaluate_date_rules()
    assert len(seen) == 1 and seen[0]["source"] == "date"


def test_date_not_evaluated_by_quote_evaluate(monkeypatch):
    """date 规则不走 evaluate(df) 主循环, 避免与 date_rule 专用路径双触发。"""
    _monkey_today(monkeypatch, "2026-08-28")
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    df = pl.DataFrame({"symbol": ["600519.SH"], "close": [1500.0]})
    assert eng.evaluate(df) == []
    assert len(eng.evaluate_date_rules()) == 1


# ── 告警引语 ────────────────────────────────────────────
def test_format_alert_quote():
    assert format_alert_quote(1500.0, 0.1) == "现价 1500.0 · +10.0%"
    assert format_alert_quote(1425.0, -0.05) == "现价 1425.0 · -5.0%"
    assert format_alert_quote(None, None) == ""          # 日期提醒等无行情
    assert format_alert_quote(None, 0.03) == "+3.0%"
    assert format_alert_quote(10.0, None) == "现价 10.0"
