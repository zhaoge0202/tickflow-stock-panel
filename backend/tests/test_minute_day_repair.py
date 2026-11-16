"""盘后当日分钟缺口回补 (services/minute_gaps.fill_day_gaps)。

背景 (2026-11-16): 盘中分钟 K 靠行情快照聚合累积, 快照断流就留下 237~239 根的
残根 —— 卡在缺口清单 0.9 容差之上, 永远进不了清单也永远补不到。原方案是盘后
无条件逐票拉全市场 240 根 (5579 次上游请求), 本函数改成只拉残根标的。

覆盖:
- 快照正常的日子: 0 次上游请求 (残根为空即跳过)
- 有残根: 只把残根 symbol 交给上游, 且一次性写盘 (复用 refresh_day_minutes)
- 当日分区缺失: 如实报 missing_partition, 不假装补过
- 上游对这些标的也没数据: 只跑一轮, 不空转
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.services import minute_gaps

_TODAY = _dt.date(2026, 11, 16)
_FULL = [f"{h:02d}:{m:02d}" for h in range(9, 12) for m in range(60)]
# 与实盘落盘同口径的完整交易日: 09:30~11:30 + 13:00~15:00 (241 根, 两个端点都含)
_BARS: list[str] = []
_t = _dt.datetime(2026, 1, 1, 9, 30)
while _t <= _dt.datetime(2026, 1, 1, 11, 30):
    _BARS.append(_t.strftime("%H:%M"))
    _t += _dt.timedelta(minutes=1)
_t = _dt.datetime(2026, 1, 1, 13, 0)
while _t <= _dt.datetime(2026, 1, 1, 15, 0):
    _BARS.append(_t.strftime("%H:%M"))
    _t += _dt.timedelta(minutes=1)


def _write_partition(minute_dir: Path, trade_date: str, bars_by_symbol: dict[str, list[str]]) -> None:
    d = minute_dir / f"date={trade_date}"
    d.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, _dt.datetime]] = []
    for symbol, bars in bars_by_symbol.items():
        rows.extend((symbol, _dt.datetime.fromisoformat(f"{trade_date}T{t}:00")) for t in bars)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "datetime": [r[1] for r in rows],
        "open": [10.0] * len(rows),
        "high": [10.0] * len(rows),
        "low": [10.0] * len(rows),
        "close": [10.0] * len(rows),
        "volume": [1.0] * len(rows),
        "amount": [1.0] * len(rows),
    }).write_parquet(d / "part.parquet")


def _repo(tmp_path: Path):
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))


def test_complete_day_issues_no_upstream_request(tmp_path, monkeypatch):
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, _TODAY.isoformat(), {"600519.SH": _BARS, "000001.SZ": _BARS})
    calls: list[dict] = []
    monkeypatch.setattr(
        minute_gaps, "refresh_day_minutes",
        lambda *a, **kw: calls.append(kw) or {"requested": 0, "fetched_symbols": 0},
    )

    r = minute_gaps.fill_day_gaps(_repo(tmp_path), None, _TODAY)

    assert calls == []
    assert r["mode"] == "gap_fill"
    assert r["rounds"] == 0
    assert r["remaining"] == 0


def test_only_residual_symbols_are_requested(tmp_path, monkeypatch):
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, _TODAY.isoformat(), {
        "600519.SH": _BARS,
        "000001.SZ": _BARS[:239],   # 239 根: 0.9 容差下"完整", 对 1.0 口径是残根
        "300750.SZ": _BARS[:120],
    })
    requested: list[list[str]] = []

    def fake_refresh(repo, capset, trade_date, *, symbols=None, concurrency=8):
        requested.append(list(symbols or []))
        return {"requested": len(symbols or []), "fetched_symbols": len(symbols or []),
                "empty": 0, "failed": 0, "written_rows": 100}

    monkeypatch.setattr(minute_gaps, "refresh_day_minutes", fake_refresh)

    r = minute_gaps.fill_day_gaps(_repo(tmp_path), None, _TODAY)

    assert len(requested) == 1
    assert sorted(requested[0]) == ["000001.SZ", "300750.SZ"]
    assert r["rounds"] == 1
    assert r["repaired"] == 2
    assert r["remaining"] == 2   # 假 refresh 没真写盘, 复扫仍是同一清单 → 无进展即停


def test_missing_partition_is_reported_not_faked(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        minute_gaps, "refresh_day_minutes",
        lambda *a, **kw: calls.append(kw) or {},
    )

    r = minute_gaps.fill_day_gaps(_repo(tmp_path), None, _TODAY)

    assert calls == []
    assert r["mode"] == "missing_partition"
    assert r["remaining"] is None


def test_upstream_empty_stops_after_one_round(tmp_path, monkeypatch):
    minute_dir = tmp_path / "kline_minute"
    _write_partition(minute_dir, _TODAY.isoformat(), {"000001.SZ": _BARS[:50]})
    rounds: list[list[str]] = []

    def fake_refresh(repo, capset, trade_date, *, symbols=None, concurrency=8):
        rounds.append(list(symbols or []))
        return {"requested": len(symbols or []), "fetched_symbols": 0,
                "empty": len(symbols or []), "failed": 0, "written_rows": 0}

    monkeypatch.setattr(minute_gaps, "refresh_day_minutes", fake_refresh)

    r = minute_gaps.fill_day_gaps(_repo(tmp_path), None, _TODAY)

    assert len(rounds) == 1          # 上游无数据时不空转到 max_rounds
    assert r["rounds"] == 1
    assert r["no_data"] == 1
    assert r["repaired"] == 0


def _minute_sync_env(monkeypatch, tmp_path):
    """run_minute_sync 的最小前置: 分钟同步开启、上游与写盘全部打桩。"""
    from app.jobs import daily_pipeline

    class _Capset:
        def has(self, capability):
            return capability.value == "kline.minute.batch"

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(daily_pipeline._prefs, "get_minute_sync_enabled", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_minute_sync_days", lambda: 5)
    monkeypatch.setattr(daily_pipeline, "_resolve_minute_symbols", lambda *_a: ["600000.SH"])
    monkeypatch.setattr(
        daily_pipeline.kline_sync, "sync_and_persist_minute", lambda *_a, **_kw: 240,
    )
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_a: None)
    return daily_pipeline, repo, _Capset()


def test_run_minute_sync_repairs_gaps_after_incremental(monkeypatch, tmp_path):
    daily_pipeline, repo, capset = _minute_sync_env(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        minute_gaps, "fill_day_gaps",
        lambda *a, **kw: calls.append(kw) or {"date": "d", "remaining": 0, "rounds": 1},
    )

    result = daily_pipeline.run_minute_sync(repo, capset)

    assert len(calls) == 1
    assert result["minute_rows"] == 240
    assert result["minute_day_repair"]["rounds"] == 1


def test_run_minute_sync_skips_repair_when_disabled(monkeypatch, tmp_path):
    daily_pipeline, repo, capset = _minute_sync_env(monkeypatch, tmp_path)
    monkeypatch.setattr(daily_pipeline.settings, "minute_gap_repair_enabled", False)
    calls: list[dict] = []
    monkeypatch.setattr(
        minute_gaps, "fill_day_gaps",
        lambda *a, **kw: calls.append(kw) or {},
    )

    result = daily_pipeline.run_minute_sync(repo, capset)

    assert calls == []
    assert result["minute_day_repair"] == {}
