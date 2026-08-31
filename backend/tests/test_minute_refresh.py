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

        # 服务门控挂 INTRADAY_UNIVERSE, 修复轮用 INTRADAY_BATCH — 两者同档, 一起授/不授
        return self._has and cap in (Cap.INTRADAY_BATCH, Cap.INTRADAY_UNIVERSE)


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
    # 交易日探针默认未知 (None → 放行): 隔离真实网络探测, holiday 分支在
    # test_trading_day.py 单独覆盖
    from app.services import trading_day
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: None)
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


# ── 两段式模式选择: 冷启动全天 → 稳态增量 ───────────────────────────


def _patch_round(monkeypatch, *, lag, inc_df, burst_df):
    calls: dict = {"modes": []}

    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: lag, raising=True,
    )
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_universe_increment",
        lambda *a, **k: (calls["modes"].append("increment"), (inc_df, 1))[1],
    )
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst",
        lambda symbols, capset, *, count=300: (calls["modes"].append("full"), (burst_df, 28))[1],
    )
    monkeypatch.setattr(
        "app.services.kline_sync._write_minute_partition",
        lambda df, minute_dir: df.height,
    )
    return calls


def _inc_df():
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "datetime": [datetime(2026, 8, 25, 10, 0)] * 2,
        "open": [10.0] * 2, "high": [10.5] * 2, "low": [9.9] * 2, "close": [10.2] * 2,
        "volume": [1000.0] * 2, "amount": [10200.0] * 2,
    })


def _full_df():
    return _inc_df()


def test_cold_start_no_local_data_uses_full_mode(tmp_path, monkeypatch):
    """当日无数据 (lag=None, 如 10 点冷启动) → 全天修复轮。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(monkeypatch, lag=None, inc_df=_inc_df(), burst_df=_full_df())
    svc._run_round()
    assert calls["modes"] == ["full"]
    st = svc.status()
    assert st["last_mode"] == "full"
    assert st["last_rows"] == 2


def test_healthy_coverage_uses_increment_mode(tmp_path, monkeypatch):
    """当日覆盖新鲜 (lag ≤ 3 分钟) → universe 单请求增量, 不打 burst。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(monkeypatch, lag=0.2, inc_df=_inc_df(), burst_df=_full_df())
    svc._run_round()
    assert calls["modes"] == ["increment"]
    st = svc.status()
    assert st["last_mode"] == "increment"
    assert st["last_requests"] == 1
    assert st["last_symbols"] == 2
    assert st["last_rows"] == 2


def test_stale_coverage_beyond_bar_headroom_falls_back_to_full(tmp_path, monkeypatch):
    """覆盖滞后超过 3 分钟 (超过 universe 3 根余量) → 全天修复轮。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(monkeypatch, lag=5.0, inc_df=_inc_df(), burst_df=_full_df())
    svc._run_round()
    assert calls["modes"] == ["full"]


def test_consecutive_empty_rounds_escalate_to_full(tmp_path, monkeypatch):
    """universe 连续 2 轮空返回 → 第 3 轮自动升级全天修复 (自愈)。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(
        monkeypatch,
        lag=0.2,
        inc_df=pl.DataFrame(),   # 增量恒空 (模拟 universe 端点持续异常)
        burst_df=_full_df(),
    )
    svc._run_round()
    svc._run_round()
    assert calls["modes"] == ["increment", "increment"]
    assert svc.status()["rounds"] == 0
    svc._run_round()
    assert calls["modes"] == ["increment", "increment", "full"]
    assert svc.status()["last_mode"] == "full"


def test_status_reports_gate_reason_when_stopped(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, enabled=False)
    st = svc.status()
    assert st["enabled"] is False
    assert st["running"] is False
    assert st["gate_reason"] == "disabled"
    assert st["interval_seconds"] == 6


# ── 偏好 ────────────────────────────────────────────────────────────


def test_refresh_preferences_defaults_and_clamp(tmp_path, monkeypatch):
    _isolated_prefs(tmp_path, monkeypatch)
    assert preferences.get_minute_refresh_enabled() is False
    assert preferences.get_minute_refresh_interval() == 6
    preferences.save({"minute_refresh_interval": 1})
    assert preferences.get_minute_refresh_interval() == 3  # 下限
    preferences.save({"minute_refresh_interval": 999})
    assert preferences.get_minute_refresh_interval() == 120  # 上限
    preferences.save({"minute_refresh_interval": 15})
    assert preferences.get_minute_refresh_interval() == 15

def test_realtime_monitor_config_owns_refresh_keys(tmp_path, monkeypatch):
    """盘中增量配置归属实时监控端点 (set_realtime_monitor_config), 并 clamp 到 [3,120]。"""
    _isolated_prefs(tmp_path, monkeypatch)
    saved = preferences.set_realtime_monitor_config({
        "minute_refresh_enabled": True,
        "minute_refresh_interval": 1,   # 越界 → clamp 到下限
    })
    assert saved["minute_refresh_enabled"] is True
    assert saved["minute_refresh_interval"] == 3
    saved = preferences.set_realtime_monitor_config({"minute_refresh_interval": 400})
    assert saved["minute_refresh_interval"] == 120
    saved = preferences.set_realtime_monitor_config({"minute_refresh_interval": 6})
    assert saved["minute_refresh_interval"] == 6


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
