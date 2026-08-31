"""实时行情轮询间隔下限契约测试。

下限语义 (中立能力原则):
- 实时源路由到插件/自定义源时, TickFlow 档位限速保护不适用 → 通用下限 1s;
- 实时源为 tickflow 时, 仍按当前订阅档位查表 (none/free=6s, starter=6s, pro=3s, expert=1s);
- 默认间隔 6s 不因路由变化而改变 (只放宽下限, 不动存量偏好)。
"""
from __future__ import annotations

from app.services.quote_service import QuoteService


def _route(monkeypatch, provider: str, tier: str) -> None:
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: provider)
    monkeypatch.setattr(QuoteService, "_current_tier", classmethod(lambda cls: tier))


def _bare() -> QuoteService:
    """绕过 __init__ (单例/线程副作用), 只用无状态方法。"""
    return QuoteService.__new__(QuoteService)


def test_custom_provider_min_interval_1s(monkeypatch):
    """插件/自定义源: 不受 TickFlow 档位保护, 下限放宽到 1s。"""
    _route(monkeypatch, "fuyao", "none")
    assert _bare().get_min_interval() == 1.0


def test_custom_provider_clamp_allows_1s(monkeypatch):
    """fuyao 路由下设置 1s 不再被抬到 6s; 超过上限仍被压回。"""
    _route(monkeypatch, "fuyao", "none")
    qs = _bare()
    assert qs._clamp_interval(1.0) == 1.0
    assert qs._clamp_interval(0.5) == 1.0
    assert qs._clamp_interval(120.0) == QuoteService.MAX_INTERVAL


def test_tickflow_tier_floor_unchanged(monkeypatch):
    """TickFlow 路由: 档位查表行为不变 (none/free/starter=6s, pro=3s, expert=1s)。"""
    for tier, expect in (("none", 6.0), ("free", 6.0), ("starter", 6.0), ("pro", 3.0), ("expert", 1.0)):
        _route(monkeypatch, "tickflow", tier)
        assert _bare().get_min_interval() == expect, tier


def test_default_interval_unchanged():
    """默认间隔仍是 6s — 放宽的只是下限, 不是默认值。"""
    assert QuoteService.DEFAULT_INTERVAL == 6.0
