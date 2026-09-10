"""final 定版确认回归: 午休/收盘定版必须校验快照时间戳, 未达边界不落盘。

实测 (2026-09-04): 收盘定版在 15:00:02 仅凭"拉取成功"即标记完成并落盘, 但
实时源当时仍返回 14:59:5x 的竞价前快照 (海鸥住工 7.07 而非官方收盘 7.10),
旧价被永久固化到当日分区; 且重启后的盘后手动刷新会再次写回旧价。修复后:

- _process_full_market_records 收到 final_boundary_ms 时, 快照最大时间戳
  达到边界 (含容差) 才落盘/评估监控, 否则只更新展示缓存;
- _final_boundary_ms/_past_final_deadline 提供边界与重试窗口 (收盘 15:00/15:30)。
"""
from __future__ import annotations

from datetime import datetime, time as dt_time

import pytest

import app.services.quote_service as qs_module
from app.market_time import CN_TZ, cn_today
from app.services.quote_service import QuoteService


def _beijing_ms(h: int, m: int, s: int = 0) -> int:
    return int(datetime.combine(cn_today(), dt_time(h, m, s), tzinfo=CN_TZ).timestamp() * 1000)


def _record(ts_ms: int) -> dict:
    return {
        "symbol": "002084.SZ",
        "last_price": 7.07,
        "open": 7.00, "high": 7.10, "low": 6.95,
        "volume": 100_000, "amount": 707_000.0,
        "timestamp": ts_ms,
    }


class _StubRepo:
    """记录写盘调用的最小仓库桩。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_index_symbol_set(self) -> set:
        return set()

    def get_etf_instruments(self):
        import polars as pl
        return pl.DataFrame()

    def flush_live_daily(self, df) -> None:
        self.calls.append("daily")

    def flush_live_daily_asset(self, asset_type: str, df) -> None:
        self.calls.append(f"daily:{asset_type}")


@pytest.fixture
def service(monkeypatch) -> tuple[QuoteService, _StubRepo, dict]:
    qs = QuoteService()
    repo = _StubRepo()
    qs._repo = repo
    events: dict[str, int] = {"broadcast": 0, "enriched": 0}
    monkeypatch.setattr(qs_module, "_persist_last_fetch", lambda ms: None)
    monkeypatch.setattr(qs, "_update_volume_delta", lambda *a, **k: None)
    monkeypatch.setattr(qs, "_evaluate_monitors", lambda *a, **k: None)
    monkeypatch.setattr(qs, "_broadcast_quote_updated", lambda: events.__setitem__("broadcast", events["broadcast"] + 1))
    monkeypatch.setattr(qs, "_flush_live_enriched", lambda *a, **k: events.__setitem__("enriched", events["enriched"] + 1))
    return qs, repo, events


def test_final_snapshot_before_boundary_skips_disk(service) -> None:
    """竞价前快照 (时间戳 < 15:00): 只更新展示缓存, 不写 daily/enriched。"""
    qs, repo, events = service
    boundary = _beijing_ms(15, 0)

    qs._process_full_market_records(
        [_record(boundary - 60_000)], t0=0.0, now_ts=0.0,
        final_boundary_ms=boundary,
    )

    assert qs._last_final_confirmed is False
    assert repo.calls == []          # 未写 kline_daily
    assert events["enriched"] == 0   # 未写 enriched
    assert events["broadcast"] == 1  # 展示缓存路径仍走通


def test_final_snapshot_after_boundary_writes(service) -> None:
    """边界后快照 (时间戳 ≥ 15:00): 定版落盘。"""
    qs, repo, events = service
    boundary = _beijing_ms(15, 0)

    qs._process_full_market_records(
        [_record(boundary + 30_000)], t0=0.0, now_ts=0.0,
        final_boundary_ms=boundary,
    )

    assert qs._last_final_confirmed is True
    assert repo.calls == ["daily"]
    assert events["enriched"] == 1
    assert events["broadcast"] == 1


def test_snapshot_without_timestamp_never_confirmed(service) -> None:
    """无时间戳的快照无法确认定版 → 不落盘 (交由盘后管道兜底)。"""
    qs, repo, events = service
    rec = _record(0)
    rec.pop("timestamp")

    qs._process_full_market_records(
        [rec], t0=0.0, now_ts=0.0, final_boundary_ms=_beijing_ms(15, 0),
    )

    assert qs._last_final_confirmed is False
    assert repo.calls == []
    assert events["enriched"] == 0


def test_normal_poll_ignores_boundary(service) -> None:
    """普通轮询 (无 final_boundary_ms): 时间戳在边界前也照常落盘。"""
    qs, repo, events = service

    qs._process_full_market_records(
        [_record(_beijing_ms(14, 59))], t0=0.0, now_ts=0.0,
    )

    assert qs._last_final_confirmed is None
    assert repo.calls == ["daily"]
    assert events["enriched"] == 1


def test_final_boundary_ms_matches_beijing_close(monkeypatch) -> None:
    fake_now = datetime.combine(cn_today(), dt_time(15, 10), tzinfo=CN_TZ)
    monkeypatch.setattr(qs_module, "cn_now", lambda: fake_now)
    assert QuoteService._final_boundary_ms("close_final") == _beijing_ms(15, 0)
    assert QuoteService._final_boundary_ms("morning_final") == _beijing_ms(11, 30)
    assert QuoteService._final_boundary_ms("afternoon") is None


def test_past_final_deadline(monkeypatch) -> None:
    def _at(h: int, m: int):
        return datetime.combine(cn_today(), dt_time(h, m), tzinfo=CN_TZ)

    monkeypatch.setattr(qs_module, "cn_now", lambda: _at(15, 29))
    assert QuoteService._past_final_deadline("close_final") is False
    monkeypatch.setattr(qs_module, "cn_now", lambda: _at(15, 30))
    assert QuoteService._past_final_deadline("close_final") is True
    monkeypatch.setattr(qs_module, "cn_now", lambda: _at(12, 9))
    assert QuoteService._past_final_deadline("morning_final") is False
    monkeypatch.setattr(qs_module, "cn_now", lambda: _at(12, 11))
    assert QuoteService._past_final_deadline("morning_final") is True
