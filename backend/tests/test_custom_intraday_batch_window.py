"""自定义数据源 (YAML) 全量分钟修复轮的当日请求窗口 — 按北京时间墙钟。

MinuteRefreshService 的修复轮经 kline_sync.fetch_intraday_custom_batch 调
GenericHTTPProvider.get_intraday_batch, 后者用 datetime.now() (服务器本地时区)
构造「当日 00:00 → 现在」窗口。仓库约定分钟时间是北京时间墙钟 (naive), 同一数据源
的其它分钟请求 (如 /api/kline/minute-batch 补拉) 发的也是北京墙钟; 在 UTC 主机上,
北京 09:45 时 datetime.now() 是 01:45, 窗口变成「00:00 → 01:45」, 整个交易时段
都拉不到分钟K。

fetch_intraday_custom_batch 对未实现 get_intraday_batch 的源回退 get_minute 时,
窗口已经用 cn_now() 构造, 这里对齐同一口径。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import market_time
from app.data_providers.custom import provider as provider_module
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.mapper import datetime_payload

_MINUTE_FIELDS = ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount")


def _host_clock(instant: datetime, host_utc_offset_hours: int) -> type[datetime]:
    """模拟某时区主机的时钟: now() 返回主机本地墙钟, now(tz) 返回该时区的同一时刻。"""
    host_tz = timezone(timedelta(hours=host_utc_offset_hours))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone(host_tz).replace(tzinfo=None)
            return instant.astimezone(tz)

    return _Clock


def _capture_window(monkeypatch: pytest.MonkeyPatch, instant: datetime, host_offset: int) -> tuple[str, str]:
    clock = _host_clock(instant, host_offset)
    monkeypatch.setattr(provider_module, "datetime", clock)
    monkeypatch.setattr(market_time, "datetime", clock)
    provider = provider_module.GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"full_minute": DatasetConfig(
            url="https://example.test/full_minute",
            field_map={name: name for name in _MINUTE_FIELDS},
        )},
    ))
    captured: dict = {}

    def request_rows(cfg, **kwargs):
        captured.update(kwargs)
        return []

    provider._request_rows = request_rows
    try:
        provider.get_intraday_batch(["600000.SH"])
    finally:
        provider.close()
    return datetime_payload(captured["start_time"]), datetime_payload(captured["end_time"])


BEIJING = timezone(timedelta(hours=8))


@pytest.mark.parametrize(
    ("beijing_now", "expected"),
    [
        (datetime(2026, 9, 16, 9, 45, tzinfo=BEIJING), ("2026-09-16T00:00:00", "2026-09-16T09:45:00")),
        (datetime(2026, 9, 16, 14, 30, tzinfo=BEIJING), ("2026-09-16T00:00:00", "2026-09-16T14:30:00")),
    ],
)
def test_intraday_batch_window_is_beijing_wallclock_on_utc_host(monkeypatch, beijing_now, expected) -> None:
    assert _capture_window(monkeypatch, beijing_now, host_offset=0) == expected


def test_intraday_batch_window_unchanged_on_beijing_host(monkeypatch) -> None:
    """北京时区主机 (如国内桌面端) 上窗口与修改前逐字一致。"""
    beijing_now = datetime(2026, 9, 16, 9, 45, tzinfo=BEIJING)
    assert _capture_window(monkeypatch, beijing_now, host_offset=8) == (
        "2026-09-16T00:00:00", "2026-09-16T09:45:00",
    )
