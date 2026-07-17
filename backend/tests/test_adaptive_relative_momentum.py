from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.strategy.builtin.adaptive_relative_momentum import MATRIX_STRATEGY
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
            "open": 8.0 if bearish else 12.0,
            "high": 8.2 if bearish else 12.2,
            "low": 7.8 if bearish else 11.8,
            "volume": 1000.0,
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


def _signals(panel: pl.DataFrame, params: dict):
    field_columns = set(panel.columns) - {"symbol", "date", "open", "high", "low", "close", "volume"}
    market = build_market_data_matrix(panel, field_columns=field_columns)
    return market, MATRIX_STRATEGY.compute_signals(market, params)


def test_matrix_strategy_keeps_only_top_cross_section():
    market, signals = _signals(_panel(), {
        "top_pct": 0.5,
        "breadth_min": 0.5,
        "min_momentum_20d": 0.0,
        "min_momentum_60d": 0.0,
    })

    selected = [symbol for symbol, hit in zip(market.symbols, signals.entry[-1], strict=True) if hit]
    assert selected == ["A", "B"]


def test_matrix_strategy_stays_in_cash_when_breadth_is_weak():
    _, signals = _signals(_panel(bearish_symbols=3), {
        "top_pct": 1.0,
        "breadth_min": 0.5,
        "min_momentum_20d": 0.0,
        "min_momentum_60d": 0.0,
    })

    assert not signals.entry.any()


def test_matrix_strategy_is_prefix_invariant():
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

    _, prefix_signals = _signals(prefix, params)
    _, extended_signals = _signals(pl.concat([prefix, future]), params)

    np.testing.assert_array_equal(extended_signals.entry[0], prefix_signals.entry[0])


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
    assert strategy.execution_backend == "matrix_native"
    assert strategy.matrix_strategy is not None
    assert strategy.filter_history_fn is None
    assert strategy.matrix_strategy.required_warmup_bars({}) == 60
    assert strategy.meta["backtest_defaults"]["max_positions"] == 8
    defaults = {param["id"]: param["default"] for param in strategy.meta["params"]}
    assert defaults["min_momentum_60d"] == 0.18
    assert strategy.max_hold_days == 10
