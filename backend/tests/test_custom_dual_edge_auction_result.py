from __future__ import annotations

import importlib
import sys
from contextlib import suppress
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.backtest.matrix import build_market_data_matrix

STRATEGY_IDS = (
    "custom_dual_edge",
    "custom_dual_edge_focus",
    "custom_dual_edge_v3",
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


def _panel(include_auction_result: bool) -> pl.DataFrame:
    start = date(2024, 1, 1)
    rows = []
    for offset in range(45):
        current = start + timedelta(days=offset)
        is_last = offset == 44
        prev_close = 10.0
        close = 10.5 if is_last else 10.0 + offset * 0.002
        open_price = 9.8 if is_last else close
        volume = 3_000.0 if is_last else 1_000.0
        row = {
            "symbol": "000001.SZ",
            "name": "测试",
            "date": current,
            "open": open_price,
            "high": 10.6 if is_last else close * 1.002,
            "low": open_price if is_last else close * 0.998,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "raw_close": close,
            "raw_high": 10.6 if is_last else close * 1.002,
        }
        if include_auction_result:
            row["auction_result_price"] = 10.35 if is_last else prev_close
            row["auction_result_volume"] = 200.0
            row["auction_result_amount"] = row["auction_result_price"] * row["auction_result_volume"]
        rows.append(row)
    return pl.DataFrame(rows)


def test_dual_edge_strategies_use_0925_auction_result_price_for_gap():
    modules = [_load_strategy_module(strategy_id) for strategy_id in STRATEGY_IDS]

    without_auction = build_market_data_matrix(_panel(False), field_columns={"amount", "raw_close", "raw_high"})
    with_auction = build_market_data_matrix(
        _panel(True),
        field_columns={
            "amount",
            "raw_close",
            "raw_high",
            "auction_result_price",
            "auction_result_volume",
            "auction_result_amount",
        },
    )

    for module in modules:
        params = {item["id"]: item.get("default") for item in module.META["params"]}
        fallback_signals = module.MATRIX_STRATEGY.compute_signals(without_auction, params)
        auction_signals = module.MATRIX_STRATEGY.compute_signals(with_auction, params)

        assert int(fallback_signals.entry[-1, 0]) == 0
        assert int(auction_signals.entry[-1, 0]) == 1
        assert auction_signals.entry_signal_ids[0] == "signal_auction"


def test_dual_edge_strategies_default_to_main_board_only():
    modules = [_load_strategy_module(strategy_id) for strategy_id in STRATEGY_IDS]

    for module in modules:
        assert module.META["basic_filter"]["boards"] == ["沪主板", "深主板"]
