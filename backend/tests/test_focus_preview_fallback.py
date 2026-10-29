"""14:50 尾盘初选的分钟K覆盖回退回归.

回归点: 分钟K由盘后任务同步 (实测 16:19 才落盘), 盘中 14:50 时 min_file 已
存在但只覆盖开盘几分钟 → 旧实现只要 min_cut 非空就直接用, 全市场被判
"未达标", preview 天天 0 条 (2026-10-28 漏掉了 603337 杰克科技)。

修复: 分钟K覆盖不到 14:50 时退回当时的 live enriched 快照。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dt_time

import polars as pl

from app.services.focus_versions import generate_preview_1450_from_context
from app.strategy.engine import StrategyDataContext, StrategyResult

TARGET = date(2026, 10, 28)


def _history() -> pl.DataFrame:
    prev = TARGET - timedelta(days=3)
    rows = []
    for symbol in ("A", "B"):
        rows.append({
            "symbol": symbol, "date": prev,
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
            "volume": 100.0, "amount": 1000.0,
            "auction_result_price": 10.0, "ma20": 9.0,
        })
        # 当日 live enriched: close 37.0 是"实时快照"的价, 与分钟线 10.8 区分开
        rows.append({
            "symbol": symbol, "date": TARGET,
            "open": 10.5, "high": 37.0, "low": 10.4, "close": 37.0,
            "volume": 200.0, "amount": 2000.0,
            "auction_result_price": 10.5, "ma20": 9.0,
        })
    return pl.DataFrame(rows)


def _context() -> StrategyDataContext:
    history = _history()
    return StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=TARGET,
        current=history.filter(pl.col("date") == TARGET),
        history=history,
    )


class _CaptureEngine:
    def __init__(self) -> None:
        self.ctx: StrategyDataContext | None = None

    def run(self, strategy_id, ctx, params=None, overrides=None):
        self.ctx = ctx
        return StrategyResult(as_of=ctx.as_of, strategy_id=strategy_id, rows=[{"symbol": "A"}], total=1)


def _write_minutes(tmp_path, stop: dt_time) -> None:
    folder = tmp_path / "kline_minute" / f"date={TARGET.isoformat()}"
    folder.mkdir(parents=True)
    rows = []
    stamp = datetime.combine(TARGET, dt_time(9, 30))
    end = datetime.combine(TARGET, stop)
    while stamp <= end:
        rows.append({
            "symbol": "A", "datetime": stamp,
            "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.8,
            "volume": 1.0, "amount": 10.0,
        })
        stamp += timedelta(minutes=1)
    pl.DataFrame(rows).write_parquet(folder / "part.parquet")


def _generate(tmp_path, engine):
    return generate_preview_1450_from_context(
        _context(), engine, "custom_dual_edge_focus", TARGET, tmp_path, {}, {},
    )


def test_minute_coverage_to_1450_uses_minute_aggregate(tmp_path):
    _write_minutes(tmp_path, dt_time(14, 50))
    engine = _CaptureEngine()

    rows = _generate(tmp_path, engine)

    assert rows == [{"symbol": "A"}]
    assert engine.ctx is not None
    assert engine.ctx.current is not None
    close = engine.ctx.current.filter(pl.col("symbol") == "A")["close"][0]
    assert close == 10.8  # 分钟聚合的 14:50 收盘价


def test_partial_minute_coverage_falls_back_to_live_snapshot(tmp_path):
    # 盘中 14:50 时分钟K只同步到 09:35: 不能拿它当全市场初选依据
    _write_minutes(tmp_path, dt_time(9, 35))
    engine = _CaptureEngine()

    rows = _generate(tmp_path, engine)

    assert rows == [{"symbol": "A"}]
    assert engine.ctx is not None
    assert engine.ctx.current is not None
    close = engine.ctx.current.filter(pl.col("symbol") == "A")["close"][0]
    assert close == 37.0  # live enriched 快照
