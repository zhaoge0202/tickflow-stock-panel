from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.factor import (
    DERIVED_FACTOR_DEPENDENCIES,
    FACTOR_COLUMNS,
    FactorBacktestService,
    FactorBatchConfig,
    FactorConfig,
)


def _panel() -> pl.DataFrame:
    rows = []
    start = date(2026, 1, 1)
    for day in range(8):
        for index, symbol in enumerate(("000001.SZ", "000002.SZ", "600000.SH")):
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=day),
                "open": 10.0 + index + day * 0.1,
                "high": 10.5 + index + day * 0.1,
                "low": 9.5 + index + day * 0.1,
                "close": 10.0 + index + day * (index + 1) * 0.1,
                "volume": 1000.0 + index * 100 + day,
                "change_pct": 0.01 * (index + 1) + day * 0.001,
                "turnover_rate": 0.02 * (3 - index) + day * 0.001,
            })
    return pl.DataFrame(rows)


class _Engine:
    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel
        self.calls: list[dict] = []

    def load_panel(self, symbols, start, end, columns, asset_type):
        self.calls.append({
            "symbols": symbols,
            "start": start,
            "end": end,
            "columns": columns,
            "asset_type": asset_type,
        })
        selected = [column for column in columns if column in self.panel.columns]
        return self.panel.select(selected)


def _batch_config(factor_names: list[str]) -> FactorBatchConfig:
    return FactorBatchConfig(
        factor_names=factor_names,
        symbols=None,
        start=date(2026, 1, 1),
        end=date(2026, 1, 8),
        n_groups=3,
        rebalance="daily",
    )


def test_batch_loads_panel_once_and_deduplicates_factors():
    engine = _Engine(_panel())
    result = FactorBacktestService(engine).run_batch(
        _batch_config(["change_pct", "turnover_rate", "change_pct"]),
    )

    assert len(engine.calls) == 1
    assert result.config["factor_names"] == ["change_pct", "turnover_rate"]
    assert [item.factor_name for item in result.results] == ["change_pct", "turnover_rate"]
    assert all(item.error is None for item in result.results)


def test_batch_isolates_a_single_factor_failure(monkeypatch):
    engine = _Engine(_panel())
    service = FactorBacktestService(engine)
    original = service._evaluate_panel

    def evaluate(panel, config, run_id, started_at, **kwargs):
        if config.factor_name == "turnover_rate":
            raise ValueError("broken factor")
        return original(panel, config, run_id, started_at, **kwargs)

    monkeypatch.setattr(service, "_evaluate_panel", evaluate)
    result = service.run_batch(_batch_config(["change_pct", "turnover_rate"]))

    assert result.results[0].error is None
    assert result.results[1].error == "broken factor"


def test_batch_empty_panel_returns_batch_error():
    engine = _Engine(pl.DataFrame())
    result = FactorBacktestService(engine).run_batch(_batch_config(["change_pct"]))

    assert len(engine.calls) == 1
    assert result.results == []
    assert result.error


def test_single_factor_contract_remains_compatible():
    engine = _Engine(_panel())
    result = FactorBacktestService(engine).run(FactorConfig(
        factor_name="change_pct",
        symbols=None,
        start=date(2026, 1, 1),
        end=date(2026, 1, 8),
        n_groups=3,
        rebalance="daily",
    ))

    assert result.error is None
    assert result.config["factor_name"] == "change_pct"
    assert result.config["asset_type"] == "stock"
    assert result.n_symbols == 3
    assert result.ic_series


def test_factor_catalog_covers_normalized_indicator_families():
    factor_ids = [item["id"] for item in FACTOR_COLUMNS]

    assert len(factor_ids) == len(set(factor_ids))
    assert len(factor_ids) > 16
    assert {
        "ma5_bias",
        "ema60_bias",
        "macd_hist_pct",
        "boll_position",
        "atr_pct",
        "kdj_d",
        "vol_ratio_10d",
        "turnover_ratio_5d",
        "log_amount",
        "gap_return",
        "distance_to_high_60d",
        "max_ret_20d",
        "ret_skew_20d",
        "up_days_20d",
        "amihud_20d",
        "turnover_z_60d",
        "vol_price_corr_20d",
        "vwap_bias",
        "vol_trend_5_60",
        "limit_up_count_20d",
        "limit_up_count_60d",
        "pb_latest",
        "roe_latest",
        "revenue_yoy_latest",
        "debt_ratio_latest",
    } <= set(factor_ids)
    assert set(DERIVED_FACTOR_DEPENDENCIES) <= set(factor_ids)


def test_derived_factors_are_computed_from_shared_base_panel():
    start = date(2026, 1, 1)
    rows = []
    for day in range(70):
        close = 10.0 + day
        rows.append({
            "symbol": "000001.SZ",
            "date": start + timedelta(days=day),
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 1000.0 + day,
            "amount": (1000.0 + day) * close,
            "turnover_rate": 2.0 + day * 0.01,
        })
    engine = _Engine(pl.DataFrame(rows))
    service = FactorBacktestService(engine)
    factor_names = [
        "ma20_bias",
        "atr_pct",
        "boll_position",
        "vol_ratio_10d",
        "turnover_ratio_5d",
        "log_amount",
        "gap_return",
        "intraday_return",
        "close_position",
        "distance_to_high_60d",
    ]

    panel = service._load_factor_panel(_batch_config(factor_names), factor_names)
    last = panel.tail(1).to_dicts()[0]

    assert set(factor_names) <= set(panel.columns)
    assert last["ma20_bias"] == pytest.approx(79.0 / 69.5 - 1)
    assert last["atr_pct"] == pytest.approx(last["atr_14"] / 79.0)
    assert last["boll_position"] == pytest.approx(
        (79.0 - last["boll_lower"]) / (last["boll_upper"] - last["boll_lower"]),
    )
    assert last["vol_ratio_10d"] == pytest.approx(1069.0 / 1063.5)
    assert last["turnover_ratio_5d"] == pytest.approx(2.69 / 2.66 - 1)
    assert last["log_amount"] == pytest.approx(float(np.log1p(1069.0 * 79.0)))
    assert last["gap_return"] == pytest.approx((79.0 * 0.99) / 78.0 - 1)
    assert last["intraday_return"] == pytest.approx(1 / 0.99 - 1)
    assert last["close_position"] == pytest.approx(2 / 3)
    assert last["distance_to_high_60d"] == pytest.approx(0.0)
