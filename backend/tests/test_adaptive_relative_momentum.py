from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.strategy.builtin.adaptive_relative_momentum import filter_history
from app.strategy.engine import StrategyEngine


def _panel(bearish_symbols: int = 0) -> pl.DataFrame:
    rows = []
    for i, symbol in enumerate(["A", "B", "C", "D"]):
        bearish = i < bearish_symbols
        momentum = 0.30 - i * 0.05
        rows.append({
            "symbol": symbol,
            "date": date(2026, 1, 2),
            "close": 8.0 if bearish else 12.0,
            "ma20": 9.0 if bearish else 11.0,
            "ma60": 10.0,
            "high_60d": 12.2,
            "momentum_5d": 0.05,
            "momentum_10d": momentum - 0.02,
            "momentum_20d": momentum,
            "momentum_60d": momentum + 0.05,
            "annual_vol_20d": 0.4,
            "vol_ratio_5d": 1.2,
        })
    return pl.DataFrame(rows)


def test_filter_history_keeps_only_top_cross_section():
    result = filter_history(_panel(), {
        "top_pct": 0.5,
        "breadth_min": 0.5,
        "min_momentum_20d": 0.0,
        "min_momentum_60d": 0.0,
    })

    assert result["symbol"].to_list() == ["A", "B"]
    assert not any(col.startswith("_") for col in result.columns)


def test_filter_history_stays_in_cash_when_breadth_is_weak():
    result = filter_history(_panel(bearish_symbols=3), {
        "top_pct": 1.0,
        "breadth_min": 0.5,
        "min_momentum_20d": 0.0,
        "min_momentum_60d": 0.0,
    })

    assert result.is_empty()


def test_filter_history_is_prefix_invariant():
    prefix = _panel()
    future = prefix.with_columns([
        (pl.col("date") + timedelta(days=1)).alias("date"),
        (pl.col("momentum_20d") * -1).alias("momentum_20d"),
        (pl.col("momentum_60d") * -1).alias("momentum_60d"),
    ])
    params = {
        "top_pct": 0.5,
        "breadth_min": 0.5,
        "min_momentum_20d": 0.0,
        "min_momentum_60d": 0.0,
    }

    prefix_hits = filter_history(prefix, params).select("symbol", "date")
    extended_hits = (
        filter_history(pl.concat([prefix, future]), params)
        .filter(pl.col("date") == prefix["date"][0])
        .select("symbol", "date")
    )

    assert extended_hits.equals(prefix_hits)


def test_strategy_explicitly_supports_etf_without_stock_board_filter():
    path = (
        Path(__file__).parents[1]
        / "app"
        / "strategy"
        / "builtin"
        / "adaptive_relative_momentum.py"
    )
    strategy = StrategyEngine._load_file(path)

    assert strategy.meta["asset_types"] == ["stock", "etf"]
    assert strategy.basic_filter["boards"] == []
    assert strategy.basic_filter["market_cap_min"] is None
    assert strategy.filter_history_fn is not None
    assert strategy.lookback_days == 120
    assert strategy.meta["backtest_defaults"]["max_positions"] == 8
    defaults = {param["id"]: param["default"] for param in strategy.meta["params"]}
    assert defaults["min_momentum_60d"] == 0.18
    assert strategy.max_hold_days == 10
