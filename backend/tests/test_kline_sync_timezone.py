"""时区契约测试 — 分时拉取窗口必须按北京时间解释, 与服务器本地时区无关。

fetch_minute_single 构造的 naive datetime 会被 _datetime_to_ms 的 .timestamp()
按服务器本地时区解释: UTC 容器 (Docker 默认) 上窗口偏移 8 小时, 补拉必为空。
"""
from __future__ import annotations

from datetime import date, datetime

from app.market_time import CN_TZ
from app.services import kline_sync


def test_fetch_minute_single_window_is_beijing_wall_clock(monkeypatch):
    captured: dict[str, int] = {}

    def _fake_try_custom_minute(*args, **kwargs):
        return (None, True)  # 未配自定义源 → 走 TickFlow 分支

    class _FakeKlines:
        @staticmethod
        def batch(symbols, period, start_time, end_time, **kwargs):
            captured["start_ms"] = start_time
            captured["end_ms"] = end_time
            return []

    class _FakeClient:
        klines = _FakeKlines

    monkeypatch.setattr(kline_sync, "_try_custom_minute", _fake_try_custom_minute)
    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient())

    kline_sync.fetch_minute_single("600000.SH", date(2026, 8, 14))

    start = datetime.fromtimestamp(captured["start_ms"] / 1000, tz=CN_TZ)
    end = datetime.fromtimestamp(captured["end_ms"] / 1000, tz=CN_TZ)
    assert (start.date(), start.hour, start.minute) == (date(2026, 8, 14), 9, 25)
    assert (end.date(), end.hour, end.minute) == (date(2026, 8, 14), 15, 5)
