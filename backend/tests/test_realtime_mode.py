"""回归测试: 实时行情模式判定 — 免费档不再提供自选实时降级。

watchlist(自选前 5 只)模式已于 2026-08 移除: 自定义实时源(如 fuyao)的
全市场快照免费且更优, TickFlow 免费档不再保留降级通路。锁定判定结果,
防止该通路被无意恢复。
"""
from app.services.quote_service import QuoteService


def test_custom_realtime_source_is_full_market(monkeypatch):
    """自定义实时源(如 fuyao)无视 TickFlow 档位, 恒为全市场。"""
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "free")
    assert QuoteService.realtime_mode() == "full_market"


def test_tickflow_free_has_no_realtime(monkeypatch):
    """TickFlow 免费档 = 无实时(不再降级为自选模式)。"""
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "free")
    assert QuoteService.realtime_mode() == "none"
    assert QuoteService.is_realtime_allowed() is False


def test_tickflow_paid_is_full_market(monkeypatch):
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "pro")
    assert QuoteService.realtime_mode() == "full_market"
