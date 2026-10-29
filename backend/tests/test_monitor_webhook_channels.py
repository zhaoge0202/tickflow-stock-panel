"""监控规则推送渠道回归.

回归点 1: 旧「策略监控」迁移生成的 type=strategy 规则不带 webhook_channels,
          normalize() 兜成 [], 告警只进应用内, 企业微信一条都发不出去。
回归点 2: channels 为空时 _maybe_send_webhook 静默 `continue`, 日志里查不出
          任何痕迹, 用户只能靠"应用内有、外部没有"反推。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.services import email_adapter, preferences, quote_service
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules


def test_migration_new_rule_inherits_default_channels(tmp_path):
    touched = monitor_rules.migrate_strategy_monitors(
        tmp_path,
        ["custom_dual_edge_focus"],
        {"custom_dual_edge_focus": "双刃合-Focus"},
        default_channels=["wecom"],
    )

    assert len(touched) == 1
    rule = monitor_rules.load_one(tmp_path, "mr_strategy_custom_dual_edge_focus")
    assert rule is not None
    assert rule["webhook_channels"] == ["wecom"]


def test_migration_keeps_existing_rule_channels(tmp_path):
    monitor_rules.save_one(tmp_path, monitor_rules.normalize({
        "id": "mr_strategy_custom_dual_edge_focus",
        "name": "策略监控 · 双刃合-Focus",
        "type": "strategy",
        "strategy_id": "custom_dual_edge_focus",
        "webhook_channels": ["feishu"],
    }))

    monitor_rules.migrate_strategy_monitors(
        tmp_path,
        ["custom_dual_edge_focus"],
        {"custom_dual_edge_focus": "双刃合-Focus"},
        default_channels=["wecom"],
    )

    rule = monitor_rules.load_one(tmp_path, "mr_strategy_custom_dual_edge_focus")
    assert rule is not None
    assert rule["webhook_channels"] == ["feishu"]


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


@pytest.fixture
def webhook_env(monkeypatch):
    """只配企业微信地址, 其余渠道留空。"""
    monkeypatch.setattr(preferences, "get_feishu_webhook_url", lambda: "")
    monkeypatch.setattr(preferences, "get_feishu_webhook_secret", lambda: "")
    monkeypatch.setattr(
        preferences, "get_wecom_webhook_url",
        lambda: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
    )
    monkeypatch.setattr(preferences, "get_custom_webhook_url", lambda: "")
    monkeypatch.setattr(preferences, "get_email_smtp_config", lambda: {})
    monkeypatch.setattr(email_adapter, "is_configured", lambda _cfg: False)
    executor = _RecordingExecutor()
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", executor)
    return executor


def _event() -> dict:
    return {
        "rule_id": "mr_strategy_custom_dual_edge_focus",
        "source": "strategy",
        "type": "buy_signal",
        "symbol": "002434.SZ",
        "name": "万里扬",
        "message": "策略「双刃合-Focus」买入信号 万里扬 +6.3%",
        "price": 6.79,
        "change_pct": 0.0626,
    }


def test_empty_channels_logs_skip_instead_of_silence(webhook_env, caplog):
    service = QuoteService.__new__(QuoteService)
    engine = SimpleNamespace(rules={
        "mr_strategy_custom_dual_edge_focus": {"webhook_channels": []},
    })

    with caplog.at_level(logging.INFO):
        service._maybe_send_webhook([_event()], engine)

    assert webhook_env.calls == []
    assert "Webhook 未投递" in caplog.text


def test_wecom_channel_enqueues_one_message(webhook_env, caplog):
    service = QuoteService.__new__(QuoteService)
    engine = SimpleNamespace(rules={
        "mr_strategy_custom_dual_edge_focus": {"webhook_channels": ["wecom"]},
    })

    with caplog.at_level(logging.INFO):
        service._maybe_send_webhook([_event()], engine)

    assert len(webhook_env.calls) == 1
    fn, args, _kwargs = webhook_env.calls[0]
    assert fn.__name__ == "send_wecom"
    assert args[0].endswith("key=test")
    assert "万里扬" in args[2]
    assert "Webhook 已提交 1 条" in caplog.text
