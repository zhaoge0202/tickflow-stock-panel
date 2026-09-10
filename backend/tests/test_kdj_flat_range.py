"""KDJ 零分母回归: 9 日内最高价=最低价时 RSV 无定义, 不得污染后续递推。

场内货币 ETF (511990/511880 等) 与长期无成交的冷门标的会连续多日
最高价=最低价, 此时 rolling_max(9)-rolling_min(9) 恰好为 0。旧实现用
fill_null(1e-12) 守卫, 但零分母不是空值, 0/0 得到 NaN 并被 ewm_mean
递推永久传染, 该标的此后所有交易日的 KDJ 都是 NaN。

矩阵路径 (backtest/matrix.matrix_feature) 对同一份数据只把该日置空、
之后继续递推, 两条路径必须给出同一套值。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix, matrix_feature
from app.indicators.pipeline import compute_indicators

FLAT_START = 12
FLAT_STOP = 21  # 12..20 共 9 个交易日 最高价=最低价
N_DAYS = 40


def _flat_window_panel() -> pl.DataFrame:
    rows: list[dict] = []
    start = date(2024, 1, 1)
    close = 10.0
    for offset in range(N_DAYS):
        if FLAT_START <= offset < FLAT_STOP:
            high = low = current = close
        else:
            close = close * (1.0 + 0.01 * np.sin(offset / 3.0))
            current = close
            high = close * 1.02
            low = close * 0.98
        rows.append({
            "symbol": "511990.SH",
            "date": start + timedelta(days=offset),
            "open": current,
            "high": high,
            "low": low,
            "close": current,
            "volume": 1000.0,
        })
    return pl.DataFrame(rows)


def test_kdj_recovers_after_flat_high_low_window():
    panel = _flat_window_panel()
    enriched = compute_indicators(panel, needed={"kdj_k", "kdj_d", "kdj_j"})

    for name in ("kdj_k", "kdj_d", "kdj_j"):
        values = enriched[name].to_numpy().astype(float)
        # 前 8 个交易日仍是预热空值, 第 9 个平盘日 (index 20) RSV 无定义。
        assert np.isnan(values[:8]).all(), name
        assert np.isnan(values[FLAT_STOP - 1]), name
        # 平盘窗口滚出后必须恢复, 而不是永久 NaN。
        assert np.isfinite(values[FLAT_STOP:]).all(), (
            f"{name} 在平盘窗口之后仍为 NaN: {values[FLAT_STOP:]}"
        )
        assert int(np.isnan(values).sum()) == 9, name


def test_kdj_cold_path_matches_matrix_on_flat_window():
    panel = _flat_window_panel()
    enriched = compute_indicators(panel, needed={"kdj_k", "kdj_d", "kdj_j"})
    market = build_market_data_matrix(panel)

    for name in ("kdj_k", "kdj_d", "kdj_j"):
        np.testing.assert_allclose(
            enriched[name].to_numpy().astype(float),
            np.asarray(matrix_feature(market, name)[:, 0], dtype=float),
            rtol=2e-4,
            atol=2e-4,
            equal_nan=True,
            err_msg=name,
        )


def test_kdj_before_flat_window_is_unchanged():
    """平盘窗口之前的取值不受本次修复影响 (锁定既有口径)。"""
    panel = _flat_window_panel()
    enriched = compute_indicators(panel, needed={"kdj_k", "kdj_d"})
    np.testing.assert_allclose(
        enriched["kdj_k"].to_numpy().astype(float)[8:20],
        [
            79.038803, 78.910706, 77.639870, 73.898865, 69.964340, 65.401917,
            59.955997, 54.126106, 50.239616, 47.648457, 46.534885, 47.689919,
        ],
        rtol=1e-5,
        atol=1e-5,
    )
