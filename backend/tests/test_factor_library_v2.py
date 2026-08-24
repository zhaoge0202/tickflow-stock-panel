"""新增因子维度的解析正确性测试 (收益形态/流动性/涨停基因/量价扩展)。

金标准路径一致性由 test_matrix_strategy.py::test_research_factor_catalog_matches_matrix_features
覆盖; 这里用可手算的小样本验证公式本身。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix, matrix_feature
from app.strategy.scoring import materialize_scoring_columns


def _panel(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _single_symbol_panel(n_days: int = 70) -> pl.DataFrame:
    start = date(2025, 1, 1)
    rows = []
    for offset in range(n_days):
        change = [0.0, 0.01, -0.02, 0.03, 0.05, -0.01, 0.02, -0.03, 0.04, 0.01][offset % 10]
        close = 10.0 if offset == 0 else rows[-1]["close"] * (1 + change)
        volume = 1000.0 + (offset % 5) * 200
        consecutive = 0
        if offset % 15 == 0:
            consecutive = 1 + (offset // 15) % 3 if offset % 30 == 0 else 1
        rows.append({
            "symbol": "000001.SZ",
            "date": start + timedelta(days=offset),
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.97,
            "close": close,
            "volume": volume,
            "amount": volume * 100.0 * close,
            "turnover_rate": 1.0 + (offset % 7) * 0.3,
            "consecutive_limit_ups": consecutive,
        })
    return _panel(rows)


def _tail_value(frame: pl.DataFrame, name: str) -> float:
    return frame.tail(1).to_dicts()[0][name]


def test_max_ret_up_days_and_limit_up_counts():
    panel = _single_symbol_panel()
    frame = materialize_scoring_columns(panel, {
        "max_ret_20d", "up_days_20d",
        "limit_up_count_20d", "limit_up_count_60d",
    })

    changes = [
        panel["close"][i] / panel["close"][i - 1] - 1
        for i in range(1, panel.height)
    ]
    window = changes[-20:]
    assert math.isclose(_tail_value(frame, "max_ret_20d"), max(window), rel_tol=1e-9)
    assert math.isclose(
        _tail_value(frame, "up_days_20d"),
        float(sum(1 for value in window if value > 0)),
        rel_tol=1e-9,
    )

    hits = [
        1 if (row["consecutive_limit_ups"] or 0) > 0 else 0
        for row in panel.iter_rows(named=True)
    ]
    assert math.isclose(_tail_value(frame, "limit_up_count_20d"), float(sum(hits[-20:])), rel_tol=1e-9)
    assert math.isclose(_tail_value(frame, "limit_up_count_60d"), float(sum(hits[-60:])), rel_tol=1e-9)


def test_amihud_and_turnover_z():
    panel = _single_symbol_panel()
    frame = materialize_scoring_columns(panel, {"amihud_20d", "turnover_z_60d"})

    rows = panel.to_dicts()
    illiq = [
        abs(rows[i]["close"] / rows[i - 1]["close"] - 1) / (rows[i]["amount"] / 1e8)
        for i in range(1, panel.height)
    ]
    assert math.isclose(_tail_value(frame, "amihud_20d"), sum(illiq[-20:]) / 20, rel_tol=1e-6)

    baseline = [rows[i]["turnover_rate"] for i in range(panel.height - 61, panel.height - 1)]
    mean = sum(baseline) / 60
    variance = sum((value - mean) ** 2 for value in baseline) / 59
    std = variance ** 0.5
    expected_z = (rows[-1]["turnover_rate"] - mean) / std
    assert math.isclose(_tail_value(frame, "turnover_z_60d"), expected_z, rel_tol=1e-6)


def test_vwap_bias_and_vol_trend():
    panel = _single_symbol_panel()
    frame = materialize_scoring_columns(panel, {"vwap_bias", "vol_trend_5_60"})

    last = panel.tail(1).to_dicts()[0]
    vwap = last["amount"] / (last["volume"] * 100.0)
    assert math.isclose(_tail_value(frame, "vwap_bias"), last["close"] / vwap - 1, rel_tol=1e-9)

    volumes = panel["volume"].to_list()
    fast = sum(volumes[-5:]) / 5
    slow = sum(volumes[-60:]) / 60
    assert math.isclose(_tail_value(frame, "vol_trend_5_60"), fast / slow - 1, rel_tol=1e-9)


def test_ret_skew_matches_population_skew():
    # 周期夹具的 20 日窗口恰含两个完整周期, 偏度恒为 0; 用不对称收益验证公式。
    start = date(2025, 3, 1)
    changes = [0.01, -0.005, -0.004, 0.09, -0.006, 0.002, -0.003, -0.002, -0.005, 0.003] * 7
    rows = []
    close = 10.0
    for offset, change in enumerate(changes):
        close = close * (1 + change)
        rows.append({
            "symbol": "000001.SZ",
            "date": start + timedelta(days=offset),
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.97,
            "close": close,
            "volume": 1000.0 + offset,
            "amount": (1000.0 + offset) * close,
            "turnover_rate": 1.0,
            "consecutive_limit_ups": 0,
        })
    panel = _panel(rows)
    frame = materialize_scoring_columns(panel, {"ret_skew_20d"})

    window = changes[-20:]
    mean = sum(window) / 20
    central_second = sum((value - mean) ** 2 for value in window) / 20
    central_third = sum((value - mean) ** 3 for value in window) / 20
    expected = central_third / central_second ** 1.5
    assert abs(expected) > 0.1
    assert math.isclose(_tail_value(frame, "ret_skew_20d"), expected, rel_tol=1e-6)


def test_vol_price_corr_matches_pearson():
    panel = _single_symbol_panel()
    frame = materialize_scoring_columns(panel, {"vol_price_corr_20d"})

    closes = panel["close"].to_list()
    changes = [closes[i] / closes[i - 1] - 1 for i in range(panel.height - 20, panel.height)]
    volumes = panel["volume"].to_list()[-20:]
    n = 20
    mean_x = sum(changes) / n
    mean_y = sum(volumes) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(changes, volumes, strict=True)) / n
    var_x = sum((x - mean_x) ** 2 for x in changes) / n
    var_y = sum((y - mean_y) ** 2 for y in volumes) / n
    expected = cov / (var_x * var_y) ** 0.5
    assert math.isclose(_tail_value(frame, "vol_price_corr_20d"), expected, rel_tol=1e-6)


def test_matrix_limit_up_counts_use_consecutive_field():
    panel = _single_symbol_panel()
    frame = materialize_scoring_columns(panel, {"limit_up_count_20d"})
    market = build_market_data_matrix(
        panel,
        field_columns={"amount", "turnover_rate", "consecutive_limit_ups"},
    )
    np.testing.assert_allclose(
        matrix_feature(market, "limit_up_count_20d")[:, 0],
        frame["limit_up_count_20d"].to_numpy(),
        rtol=1e-6,
        atol=1e-6,
        equal_nan=True,
    )
