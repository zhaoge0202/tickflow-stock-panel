"""盘中分钟增量刷新服务 (minute_refresh) 测试。

覆盖:
- 连续竞价时段判定 (含边界)
- 门控链: 开关关闭 / 自定义分钟源让位 / 能力缺失 / 时段外 / 放行
- 单轮: mock 边界层脉冲 + 落盘, 校验状态字段与 universe 来源
- 偏好读写: 默认关闭、间隔 clamp [60, 300]
- API: /minute-refresh/status 无服务时 available=false

不发起真实网络请求: fetch_intraday_full_market_burst 与 _write_minute_partition
均 monkeypatch 替换。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl

from app.services import minute_refresh, preferences
from app.services.minute_refresh import MinuteRefreshService, _in_continuous_session


def _isolated_prefs(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    return path


class _FakeCapSet:
    def __init__(self, has_intraday_batch: bool):
        self._has = has_intraday_batch

    def has(self, cap) -> bool:
        from app.tickflow.capabilities import Cap

        return self._has and cap == Cap.INTRADAY_BATCH


class _FakeAppState:
    def __init__(self, has_intraday_batch: bool):
        self.capabilities = _FakeCapSet(has_intraday_batch)


class _FakeRepo:
    def __init__(self, symbols: list[str]):
        from pathlib import Path
        self._inst = pl.DataFrame({"symbol": symbols})
        self.store = type("S", (), {"data_dir": Path(".")})()

    def get_instruments(self) -> pl.DataFrame:
        return self._inst


# ── 时段判定 ────────────────────────────────────────────────────────


def test_continuous_session_boundaries():
    wk = datetime(2026, 8, 25, 10, 0)  # 周二
    assert _in_continuous_session(wk)
    assert not _in_continuous_session(datetime(2026, 8, 25, 9, 29))
    assert not _in_continuous_session(datetime(2026, 8, 25, 11, 31))   # 午休
    assert _in_continuous_session(datetime(2026, 8, 25, 13, 0))        # 午后恢复
    assert _in_continuous_session(datetime(2026, 8, 25, 15, 0))        # 收盘瞬时
    assert not _in_continuous_session(datetime(2026, 8, 25, 15, 1))
    assert not _in_continuous_session(datetime(2026, 8, 22, 10, 0))    # 周六


# ── 门控链 ──────────────────────────────────────────────────────────


def _svc(tmp_path, monkeypatch, *, enabled=True, custom_provider=False, capability=True, in_hours=True):
    _isolated_prefs(tmp_path, monkeypatch)
    preferences.save({"minute_refresh_enabled": enabled})
    if custom_provider:
        # 模拟已注册的自定义分钟源 (真实注册表在测试环境未加载)
        monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "a-stock-data")
    svc = MinuteRefreshService(_FakeRepo(["600000.SH"]))
    svc.set_app_state(_FakeAppState(capability))
    monkeypatch.setattr(minute_refresh, "_in_continuous_session", lambda now=None: in_hours)
    return svc


def test_gate_disabled(tmp_path, monkeypatch):
    assert _svc(tmp_path, monkeypatch, enabled=False)._gate_reason() == "disabled"


def test_gate_custom_provider_yields(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, custom_provider=True)
    assert svc._gate_reason() == "custom_minute_provider"


def test_gate_capability_missing(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, capability=False)
    assert svc._gate_reason() == "capability"


def test_gate_outside_trading_hours(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, in_hours=False)
    assert svc._gate_reason() == "outside_trading_hours"


def test_gate_pass(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    assert svc._gate_reason() is None
    assert svc.capability_ok() and not svc.custom_provider_active()


# ── 单轮 ────────────────────────────────────────────────────────────


def test_run_round_writes_partition_and_updates_status(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    minute_df = pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 8, 25, 1, 30)],
        "open": [10.0], "high": [10.5], "low": [9.9], "close": [10.2],
        "volume": [1000.0], "amount": [10200.0],
    })
    calls: dict = {}

    def fake_burst(symbols, capset, *, count=300):
        calls["symbols"] = list(symbols)
        return (minute_df, 1)

    def fake_write(df, minute_dir):
        calls["dir"] = minute_dir
        calls["rows"] = df.height
        return df.height

    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst", fake_burst
    )
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition", fake_write)

    svc._run_round()

    assert calls["symbols"] == ["600000.SH"]
    assert calls["rows"] == 1
    st = svc.status()
    assert st["rounds"] == 1
    assert st["last_rows"] == 1
    assert st["last_symbols"] == 1
    assert st["last_requests"] == 1
    assert st["last_round_at"] is not None
    assert st["last_error"] is None
    assert st["capability_ok"] is True


def test_run_round_records_error_when_burst_empty(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst",
        lambda symbols, capset, *, count=300: (pl.DataFrame(), 3),
    )
    svc._run_round()
    st = svc.status()
    assert st["rounds"] == 0
    assert "no data" in st["last_error"]
    assert st["last_requests"] == 3


def test_status_reports_gate_reason_when_stopped(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, enabled=False)
    st = svc.status()
    assert st["enabled"] is False
    assert st["running"] is False
    assert st["gate_reason"] == "disabled"
    assert st["interval_seconds"] == 60


# ── 偏好 ────────────────────────────────────────────────────────────


def test_refresh_preferences_defaults_and_clamp(tmp_path, monkeypatch):
    _isolated_prefs(tmp_path, monkeypatch)
    assert preferences.get_minute_refresh_enabled() is False
    assert preferences.get_minute_refresh_interval() == 60
    preferences.save({"minute_refresh_interval": 5})
    assert preferences.get_minute_refresh_interval() == 60  # 下限
    preferences.save({"minute_refresh_interval": 999})
    assert preferences.get_minute_refresh_interval() == 300  # 上限
    preferences.save({"minute_refresh_interval": 90})
    assert preferences.get_minute_refresh_interval() == 90

def test_realtime_monitor_config_owns_refresh_keys(tmp_path, monkeypatch):
    """盘中增量配置归属实时监控端点 (set_realtime_monitor_config), 并 clamp 到 [60,300]。"""
    _isolated_prefs(tmp_path, monkeypatch)
    saved = preferences.set_realtime_monitor_config({
        "minute_refresh_enabled": True,
        "minute_refresh_interval": 10,   # 越界 → clamp 到下限
    })
    assert saved["minute_refresh_enabled"] is True
    assert saved["minute_refresh_interval"] == 60
    saved = preferences.set_realtime_monitor_config({"minute_refresh_interval": 400})
    assert saved["minute_refresh_interval"] == 300
    saved = preferences.set_realtime_monitor_config({"minute_refresh_interval": 120})
    assert saved["minute_refresh_interval"] == 120


def test_status_endpoint_without_service():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/api/settings/minute-refresh/status")
    assert resp.status_code == 200
    assert resp.json() == {"available": False}
