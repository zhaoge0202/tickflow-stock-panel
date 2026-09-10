"""时区契约测试 — 分时拉取窗口必须按北京时间解释, 与服务器本地时区无关。

fetch_minute_single 构造的 naive datetime 会被 _datetime_to_ms 的 .timestamp()
按服务器本地时区解释: UTC 容器 (Docker 默认) 上窗口偏移 8 小时, 补拉必为空。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl

from app.market_time import CN_TZ
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


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

    kline_sync.fetch_minute_single(
        "600000.SH",
        date(2026, 8, 14),
        capset=CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()}),
    )

    start = datetime.fromtimestamp(captured["start_ms"] / 1000, tz=CN_TZ)
    end = datetime.fromtimestamp(captured["end_ms"] / 1000, tz=CN_TZ)
    assert (start.date(), start.hour, start.minute) == (date(2026, 8, 14), 9, 25)
    assert (end.date(), end.hour, end.minute) == (date(2026, 8, 14), 15, 5)


def _capture_minute_window(monkeypatch, tmp_path, local_dt: datetime, **kwargs) -> dict:
    """桩掉 sync_and_persist_minute 的外部依赖, 只截获传给取数层的时间窗口。

    local_dt 模拟 kline_minute 里的极值 (落盘口径为北京墙钟 naive)。
    """
    captured: dict = {}

    def _fake_sync_minute_batch(symbols, **call_kwargs):
        captured.update(call_kwargs)
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda repo: None)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *a, **kw: SimpleNamespace(batch=100, rpm=30))
    monkeypatch.setattr(kline_sync, "sync_minute_batch", _fake_sync_minute_batch)

    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.execute_one = lambda sql: (local_dt,)
    kline_sync.sync_and_persist_minute(
        ["600000.SH"],
        repo,
        CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()}),
        **kwargs,
    )
    return captured


def test_sync_and_persist_minute_incremental_window_is_beijing_wall_clock(monkeypatch, tmp_path):
    """增量分钟同步: 起点取自本地分钟 K 的北京墙钟, 终点必须同口径。

    终点用服务器本地墙钟时, _datetime_to_ms 会把两端按同一本地时区换算 →
    UTC 容器上起点(北京 14:30)反而晚于终点, 一个请求都发不出去, 分钟 K 永远
    停在首次拉取的位置。
    """
    local_latest = datetime(2026, 8, 14, 14, 30)  # 本地最新分钟 K: 北京墙钟 naive

    captured = _capture_minute_window(monkeypatch, tmp_path, local_latest)
    start, end = captured["start_time"], captured["end_time"]

    assert start.utcoffset() == timedelta(hours=8)
    assert end.utcoffset() == timedelta(hours=8)
    assert start < end
    assert datetime.fromtimestamp(
        kline_sync._datetime_to_ms(start) / 1000, tz=CN_TZ
    ) == local_latest.replace(tzinfo=CN_TZ)


def test_sync_and_persist_minute_extend_backward_window_is_beijing_wall_clock(monkeypatch, tmp_path):
    """向前扩展: 终点取自本地最早分钟 K 的北京墙钟, 同样不能被服务器时区重解释。"""
    local_earliest = datetime(2026, 8, 14, 9, 30)

    captured = _capture_minute_window(
        monkeypatch, tmp_path, local_earliest, days=5, extend_backward=True,
    )
    start, end = captured["start_time"], captured["end_time"]

    assert start.utcoffset() == timedelta(hours=8)
    assert end.utcoffset() == timedelta(hours=8)
    assert end - start == timedelta(days=7)  # 5 交易日 x 7/5
    assert datetime.fromtimestamp(
        kline_sync._datetime_to_ms(end) / 1000, tz=CN_TZ
    ) == local_earliest.replace(tzinfo=CN_TZ)
