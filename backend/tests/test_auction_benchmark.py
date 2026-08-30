"""盘前风向标服务测试 (不依赖真实网络)。

覆盖: 交易日回退、历史日 JSON 缓存命中与落盘、收益 enrich 数学 (当日oc/全天/次日)、
fuyao 未配置降级、目标日失败 fallback_prev、彻底失败 no_data、AI 复盘摘要段。
日期用 2026-08-26/27/28 (写作时为过去交易日), 与仓库既有绝对日期测试风格一致。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.plugins.fuyao.client import FuyaoError
from app.services import auction_benchmark as ab


def _write_kline(data_dir: Path, day: str, rows: list[tuple[str, float, float]]) -> None:
    part = data_dir / "kline_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )
    df.write_parquet(part / "part-0.parquet")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    for d in ("2026-08-26", "2026-08-27", "2026-08-28"):
        (tmp_path / "kline_daily" / f"date={d}").mkdir(parents=True, exist_ok=True)
    _write_kline(tmp_path, "2026-08-26", [("600519.SH", 1690.0, 1700.0), ("000858.SZ", 130.0, 131.0)])
    _write_kline(tmp_path, "2026-08-27", [("600519.SH", 1717.0, 1734.0), ("000858.SZ", 132.0, 130.0)])
    _write_kline(tmp_path, "2026-08-28", [("600519.SH", 1734.0, 1768.68), ("000858.SZ", 129.0, 133.0)])
    return tmp_path


class _FakeProvider:
    """记录调用; fail_dates 中的日期抛 FuyaoError。"""

    def __init__(self, fail_dates: set[str] | None = None):
        self.calls: list[str | None] = []
        self.fail_dates = fail_dates or set()

    def short_term_benchmark(self, date_iso: str | None) -> dict:
        self.calls.append(date_iso)
        if date_iso in self.fail_dates:
            raise FuyaoError(f"code=3002: {date_iso} 未就绪")
        return {
            "date": date_iso or "2026-08-28",
            "date_ms": 0,
            "item": [
                {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
                 "auction_pct": 1.0, "tags": ["白酒", "超级品牌"]},
                {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液",
                 "auction_pct": -2.5, "tags": ["白酒"]},
            ],
        }


def _use_provider(monkeypatch, provider) -> _FakeProvider:
    monkeypatch.setattr(ab, "_provider", lambda: provider)
    return provider


# ---- 交易日解析 ----

def test_resolve_rolls_back_non_trading_day(data_dir):
    assert ab.resolve_trade_date(data_dir, date(2026, 8, 30)) == date(2026, 8, 28)
    assert ab.resolve_trade_date(data_dir, date(2026, 8, 27)) == date(2026, 8, 27)


# ---- 状态与缓存 ----

def test_source_unavailable_without_fuyao(data_dir, monkeypatch):
    monkeypatch.setattr(ab, "_provider", lambda: None)
    out = ab.get_auction_benchmark(data_dir, None)
    assert out["state"] == "source_unavailable"


def test_fetch_stores_cache_then_hits_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    out = ab.get_auction_benchmark(data_dir, None)  # 默认 → 最近分区 08-28
    assert out["state"] == "ok" and out["trade_date"] == "2026-08-28"
    assert out["count"] == 2 and len(out["items"]) == 2
    assert provider.calls == ["2026-08-28"]
    assert (data_dir / "auction_benchmark" / "date=2026-08-28.json").exists()

    provider.calls.clear()
    out2 = ab.get_auction_benchmark(data_dir, date(2026, 8, 30))  # 周日 → 08-28 → 命中缓存
    assert out2["state"] == "ok" and out2["trade_date"] == "2026-08-28"
    assert provider.calls == []


def test_explicit_history_date_uses_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    ab.get_auction_benchmark(data_dir, date(2026, 8, 27))
    assert provider.calls == ["2026-08-27"]
    provider.calls.clear()
    ab.get_auction_benchmark(data_dir, date(2026, 8, 27))
    assert provider.calls == []


def test_failure_falls_back_to_prev(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider(fail_dates={"2026-08-28"}))
    out = ab.get_auction_benchmark(data_dir, date(2026, 8, 28))
    assert out["state"] == "fallback_prev"
    assert out["trade_date"] == "2026-08-27"
    assert out["requested_date"] == "2026-08-28"
    # 回退日缓存以 ok 落盘 (不污染直查)
    cached = json.loads((data_dir / "auction_benchmark" / "date=2026-08-27.json").read_text(encoding="utf-8"))
    assert cached["state"] == "ok"


def test_total_failure_returns_no_data(data_dir, monkeypatch):
    _use_provider(monkeypatch, _FakeProvider(fail_dates={"2026-08-28", "2026-08-27"}))
    out = ab.get_auction_benchmark(data_dir, date(2026, 8, 28))
    assert out["state"] == "no_data"
    assert "2026-08-28" in out.get("message", "")


def test_corrupt_cache_refetches(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    cache = data_dir / "auction_benchmark" / "date=2026-08-28.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{broken json", encoding="utf-8")
    out = ab.get_auction_benchmark(data_dir, date(2026, 8, 28))
    assert out["state"] == "ok"
    assert provider.calls  # 缓存损坏 → 重新拉取


# ---- 收益 enrich ----

def test_enrich_math_with_local_kline(data_dir, monkeypatch):
    # 显式查 08-27: prev=08-26, next=08-28
    _use_provider(monkeypatch, _FakeProvider())
    out = ab.get_auction_benchmark(data_dir, date(2026, 8, 27))
    by = {i["thscode"]: i for i in out["items"]}
    mt = by["600519.SH"]
    # day0_oc = 1734/1717-1; day0_pct = 1734/1700-1; d1 = 1768.68/1734-1
    assert mt["day0_oc"] == pytest.approx(1734.0 / 1717.0 - 1)
    assert mt["day0_pct"] == pytest.approx(1734.0 / 1700.0 - 1)
    assert mt["d1_pct"] == pytest.approx(1768.68 / 1734.0 - 1)
    wly = by["000858.SZ"]
    assert wly["day0_oc"] == pytest.approx(130.0 / 132.0 - 1)
    assert wly["d1_pct"] == pytest.approx(133.0 / 130.0 - 1)
    # 原始字段透传
    assert mt["auction_pct"] == 1.0 and mt["tags"] == ["白酒", "超级品牌"]


def test_enrich_missing_kline_gives_none(data_dir, monkeypatch):
    # 最新分区 08-28 无次日 → d1_pct=None; kline 行存在则 oc/pct 正常
    _use_provider(monkeypatch, _FakeProvider())
    out = ab.get_auction_benchmark(data_dir, None)
    for i in out["items"]:
        assert i["d1_pct"] is None
        assert i["day0_oc"] is not None


# ---- AI 复盘摘要 ----

def test_build_recap_context_contains_summary(data_dir, monkeypatch):
    _use_provider(monkeypatch, _FakeProvider())
    ctx = ab.build_recap_context(data_dir)
    assert "盘前风向标名单" in ctx and "贵州茅台" in ctx
    assert "白酒" in ctx  # 概念标签
    assert "当日" in ctx  # 收益对照


def test_build_recap_context_empty_without_source(data_dir, monkeypatch):
    monkeypatch.setattr(ab, "_provider", lambda: None)
    assert ab.build_recap_context(data_dir) == ""
