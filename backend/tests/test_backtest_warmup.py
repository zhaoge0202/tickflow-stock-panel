"""#201 回归: 旧信号回测的 _load_panel 必须带指标 warmup 窗口。

直接按 [start,end] 过滤后 compute_all, 区间头部的 MA/MACD/RSI 会因缺
历史窗口而失真 (回测起始段信号不可信)。
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import polars as pl

from app.services.backtest import BacktestService


def _synthetic_enriched(n_days: int) -> pl.DataFrame:
    base = date(2026, 1, 1)
    days = [base + timedelta(days=i) for i in range(n_days)]
    n = len(days)
    closes = [10.0 + (i % 7) * 0.3 + i * 0.01 for i in range(n)]
    return pl.DataFrame(
        {
            "symbol": ["600000.SH"] * n,
            "date": days,
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [10000.0] * n,
            "amount": [c * 10000.0 for c in closes],
            "raw_close": closes,
            "raw_high": [c + 0.1 for c in closes],
            "raw_low": [c - 0.1 for c in closes],
        }
    )


def test_load_panel_warms_up_indicators(monkeypatch) -> None:
    df = _synthetic_enriched(250)
    monkeypatch.setattr(
        "app.services.backtest.scan_enriched_parquet", lambda glob: df.lazy()
    )
    svc = BacktestService(repo=MagicMock())

    start = df["date"][-30]
    end = df["date"][-1]
    panel = svc._load_panel(["600000.SH"], start, end)

    # warmup 行不进入结果面板 (pandas datetime64 与 date 直接比较会类型不符)
    assert str(panel["date"].min())[:10] == start.isoformat()
    assert str(panel["date"].max())[:10] == end.isoformat()
    # 区间首日的指标已有历史窗口可用, 不再是 NaN
    first = panel.iloc[0]
    assert first["rsi_14"] == first["rsi_14"]  # NaN != NaN


def test_load_panel_insufficient_history_degrades_gracefully(monkeypatch) -> None:
    # 数据起点晚于 warmup 起点时自然退化: 有多少算多少, 不抛异常
    df = _synthetic_enriched(20)
    monkeypatch.setattr(
        "app.services.backtest.scan_enriched_parquet", lambda glob: df.lazy()
    )
    svc = BacktestService(repo=MagicMock())

    panel = svc._load_panel(["600000.SH"], df["date"][0], df["date"][-1])
    assert len(panel) == 20
