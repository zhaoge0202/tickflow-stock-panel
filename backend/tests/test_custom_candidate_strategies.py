from __future__ import annotations

import importlib
import sys
from contextlib import suppress
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix, validate_signal_matrix

STRATEGY_IDS = (
    "custom_auction_leader",
    "custom_broken_board_new_leader",
    "custom_pullback_reclaim_v2",
    "custom_breakout_quality",
    "custom_trend_follow_v2",
    "custom_dual_edge_prime",
    "custom_volume_contraction_reversal",
    "custom_oscillation_reversal",
)


def _load_strategy_module(strategy_id: str):
    strategy_dir = Path(__file__).resolve().parents[2] / "data" / "strategies" / "custom"
    sys.path.insert(0, str(strategy_dir))
    try:
        sys.modules.pop(strategy_id, None)
        return importlib.import_module(strategy_id)
    finally:
        with suppress(ValueError):
            sys.path.remove(str(strategy_dir))


def _panel() -> pl.DataFrame:
    rows = []
    symbols = ("000001.SZ", "000002.SZ", "600000.SH")
    for offset in range(80):
        current = date(2025, 1, 2) + timedelta(days=offset)
        for asset_id, symbol in enumerate(symbols):
            close = 10.0 + offset * 0.03 + asset_id
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "date": current,
                "open": close * 0.995,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 100_000.0 + offset * 100.0,
                "amount": close * (100_000.0 + offset * 100.0),
                "consecutive_limit_ups": 0,
                "auction_result_price": close * 0.998,
                "auction_result_volume": 1_000.0,
                "auction_result_amount": close * 0.998 * 1_000.0,
            })
    return pl.DataFrame(rows)


def test_new_candidate_strategies_load_and_emit_valid_signal_matrices():
    panel = _panel()
    market = build_market_data_matrix(
        panel,
        field_columns={
            "amount",
            "consecutive_limit_ups",
            "auction_result_price",
            "auction_result_volume",
            "auction_result_amount",
        },
    )

    for strategy_id in STRATEGY_IDS:
        module = _load_strategy_module(strategy_id)
        params = {item["id"]: item.get("default") for item in module.META["params"]}
        signals = module.MATRIX_STRATEGY.compute_signals(market, params)
        validate_signal_matrix(signals, market.shape)
        assert signals.entry_signal_ids
        assert signals.exit_signal_ids


def test_volume_contraction_reversal_detects_narrowing_decline_and_rebound():
    rows = []
    closes = [13.0] * 70 + [14.5, 14.0, 13.6, 13.3, 13.45]
    volumes = [100_000.0] * 70 + [260_000.0, 220_000.0, 218_000.0, 220_000.0, 221_000.0]
    for offset, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        rows.append({
            "symbol": "000001.SZ",
            "name": "测试股票",
            "date": date(2025, 1, 2) + timedelta(days=offset),
            "open": close - (0.03 if offset == len(closes) - 1 else 0.01),
            "high": close + (0.01 if offset == len(closes) - 1 else 0.10),
            "low": close - 0.05,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        })
    market = build_market_data_matrix(pl.DataFrame(rows), field_columns={"amount"})
    module = _load_strategy_module("custom_volume_contraction_reversal")
    params = {item["id"]: item.get("default") for item in module.META["params"]}
    signals = module.MATRIX_STRATEGY.compute_signals(market, params)
    validate_signal_matrix(signals, market.shape)
    assert bool(signals.entry[-1, 0])
    assert int(np.count_nonzero(signals.entry)) == 1


def test_broken_board_new_leader_requires_same_day_leader_break():
    rows = []
    start = date(2025, 1, 2)
    for offset in range(12):
        current = start + timedelta(days=offset)
        is_event = offset == 10
        for symbol in ("000001.SZ", "000002.SZ"):
            is_leader = symbol == "000001.SZ"
            if is_leader:
                streak = 4 if offset == 9 else (0 if is_event else max(0, offset - 5))
                limit_up = streak > 0 and not is_event
            else:
                streak = 2 if is_event else (1 if offset == 9 else 0)
                limit_up = streak > 0
            close = 10.0 + offset * 0.1 + (1.0 if is_leader else 0.0)
            volume = 20_000_000.0 if (symbol == "000002.SZ" and is_event) else 10_000_000.0
            rows.append(
                {
                    "symbol": symbol,
                    "name": "测试",
                    "date": current,
                    "open": close * 0.99,
                    "high": close,
                    "low": close * 0.98,
                    "close": close,
                    "volume": volume,
                    "amount": close * volume,
                    "signal_limit_up": limit_up,
                    "consecutive_limit_ups": streak,
                }
            )

    market = build_market_data_matrix(
        pl.DataFrame(rows),
        field_columns={"amount", "consecutive_limit_ups"},
    )
    module = _load_strategy_module("custom_broken_board_new_leader")
    params = {item["id"]: item.get("default") for item in module.META["params"]}
    signals = module.MATRIX_STRATEGY.compute_signals(market, params)
    validate_signal_matrix(signals, market.shape)

    candidate_index = market.symbols.index("000002.SZ")
    leader_index = market.symbols.index("000001.SZ")
    assert bool(signals.entry[10, candidate_index])
    assert not bool(signals.entry[10, leader_index])
    assert int(np.count_nonzero(signals.entry)) == 1
