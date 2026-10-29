"""告警价格刷新回归: 推送价不能落后全市场快照一轮.

全市场快照一轮 20s+, 告警里的 price 是快照价。出票后再补拉一次「触发标的」
的行情, 把价格更新到最新, 并同步重算默认文案, 避免推送正文与字段互相矛盾。
"""
from __future__ import annotations

from app.services.quote_service import QuoteService
from app.strategy.monitor import MonitorRuleEngine


class _Engine:
    """最小引擎替身: 只提供规则表和文案重算。"""

    def __init__(self, rules: dict) -> None:
        self.rules = rules
        self._inner = MonitorRuleEngine()

    def rebuild_default_message(self, rule: dict, ev: dict) -> str:
        return self._inner.rebuild_default_message(rule, ev)


def _service(monkeypatch, records):
    service = QuoteService.__new__(QuoteService)
    calls: list[list[str]] = []

    def fake_fetch(_self, symbols):
        calls.append(list(symbols))
        return records

    monkeypatch.setattr(QuoteService, "_fetch_symbol_quotes", fake_fetch)
    return service, calls


def _event(**overrides) -> dict:
    event = {
        "rule_id": "r1",
        "source": "strategy",
        "type": "buy_signal",
        "symbol": "002434.SZ",
        "name": "万里扬",
        "message": "策略「双刃合-Focus」买入信号 万里扬 +6.3%",
        "price": 6.79,
        "change_pct": 0.0626,
        "signals": [],
        "conditions": [],
    }
    event.update(overrides)
    return event


def _rule(**overrides) -> dict:
    rule = {
        "id": "r1",
        "name": "策略监控 · 双刃合-Focus",
        "type": "strategy",
        "strategy_id": "custom_dual_edge_focus",
        "message": "",
    }
    rule.update(overrides)
    return rule


def test_refresh_overwrites_price_and_rebuilds_default_message(monkeypatch):
    service, calls = _service(monkeypatch, [{
        "symbol": "002434.SZ",
        "last_price": 6.85,
        "change_pct": 0.072,
        "timestamp": 1793250000000,
    }])

    events = service._refresh_alert_quotes([_event()], _Engine({"r1": _rule()}))

    assert calls == [["002434.SZ"]]
    assert events[0]["price"] == 6.85
    assert events[0]["change_pct"] == 0.072
    assert events[0]["quote_ts"] == 1793250000000
    assert "+7.2%" in events[0]["message"]
    assert "+6.3%" not in events[0]["message"]


def test_custom_message_is_not_rewritten(monkeypatch):
    service, _calls = _service(monkeypatch, [{
        "symbol": "002434.SZ",
        "last_price": 6.85,
        "change_pct": 0.072,
        "timestamp": 1793250000000,
    }])
    rule = _rule(message="我自己的文案")

    events = service._refresh_alert_quotes([_event(message="我自己的文案")], _Engine({"r1": rule}))

    assert events[0]["price"] == 6.85
    assert events[0]["message"] == "我自己的文案"


def test_no_quote_returned_keeps_snapshot_price(monkeypatch):
    service, _calls = _service(monkeypatch, [])
    events = service._refresh_alert_quotes([_event()], _Engine({"r1": _rule()}))

    assert events[0]["price"] == 6.79
    assert events[0]["change_pct"] == 0.0626
    assert "quote_ts" not in events[0]


def test_batch_event_without_symbol_skips_fetch(monkeypatch):
    service, calls = _service(monkeypatch, [])
    events = service._refresh_alert_quotes([_event(symbol="")], _Engine({"r1": _rule()}))

    assert calls == []
    assert events[0]["symbol"] == ""
