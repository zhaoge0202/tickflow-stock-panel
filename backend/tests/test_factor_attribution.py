"""回测因子归因 (v1) — _factor_attribution_summary 单元测试。

覆盖:
  - 盈利/亏损单因子均值、样本数计算
  - snapshot 日期列 date/str 两种 dtype 均可关联
  - entry_signal_date 缺失时回退 entry_date
  - 无可关联行 / 空成交 / 无因子列 → None (fail-open)
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.backtest.strategy import _factor_attribution_summary


def _trade(symbol: str, day: str, pnl: float):
    return SimpleNamespace(
        symbol=symbol,
        entry_signal_date=day,
        entry_date=day,
        pnl_pct=pnl,
    )


def _snapshot(dates_as: str = "str") -> pl.DataFrame:
    frame = pl.DataFrame({
        "symbol": ["000001", "000002", "000003", "000004"],
        "date": ["2026-01-05"] * 4,
        "momentum_20d": [0.10, -0.05, 0.20, 0.00],
        "turnover_rate": [5.0, 8.0, 6.0, 7.0],
    })
    if dates_as == "date":
        frame = frame.with_columns(pl.col("date").str.to_date())
    return frame


def test_summary_win_lose_means():
    trades = [
        _trade("000001", "2026-01-05", 0.10),   # 盈利: mom 0.10, to 5
        _trade("000003", "2026-01-05", 0.05),   # 盈利: mom 0.20, to 6
        _trade("000002", "2026-01-05", -0.03),  # 亏损: mom -0.05, to 8
        _trade("000004", "2026-01-05", -0.08),  # 亏损: mom 0.00, to 7
    ]
    result = _factor_attribution_summary(_snapshot(), trades)
    assert result is not None
    assert result["n_win"] == 2 and result["n_lose"] == 2
    by_factor = {f["factor"]: f for f in result["factors"]}
    assert by_factor["momentum_20d"]["win_mean"] == round((0.10 + 0.20) / 2, 6)
    assert by_factor["momentum_20d"]["lose_mean"] == round((-0.05 + 0.00) / 2, 6)
    assert by_factor["turnover_rate"]["win_mean"] == 5.5
    assert by_factor["turnover_rate"]["lose_n"] == 2


def test_summary_accepts_date_dtype_snapshot():
    trades = [_trade("000001", "2026-01-05", 0.1), _trade("000002", "2026-01-05", -0.1)]
    result = _factor_attribution_summary(_snapshot(dates_as="date"), trades)
    assert result is not None
    assert result["n_win"] == 1


def test_summary_falls_back_to_entry_date():
    trade = SimpleNamespace(symbol="000001", entry_signal_date=None,
                            entry_date=date(2026, 1, 5), pnl_pct=0.2)
    result = _factor_attribution_summary(_snapshot(), [trade])
    assert result is not None
    assert result["factors"][0]["win_n"] == 1


def test_summary_returns_none_when_no_overlap():
    trades = [_trade("600000", "2026-02-10", 0.1)]  # 不在快照里
    assert _factor_attribution_summary(_snapshot(), trades) is None


def test_summary_returns_none_on_empty_inputs():
    assert _factor_attribution_summary(_snapshot(), []) is None
    no_factor = pl.DataFrame({"symbol": ["000001"], "date": ["2026-01-05"]})
    assert _factor_attribution_summary(no_factor, [_trade("000001", "2026-01-05", 0.1)]) is None
