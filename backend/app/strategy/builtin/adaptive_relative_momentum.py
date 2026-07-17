"""截面动量加速：市场状态过滤后，轮动到绝对动量最强的少数标的。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_shift as shift,
)

META = {
    "id": "adaptive_relative_momentum",
    "name": "截面动量加速",
    "description": "趋势广度择时 + 20/60日绝对动量 + 截面相对强度轮动，股票与ETF通用",
    "tags": ["动量", "相对强弱", "趋势", "股票ETF"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "backtest_defaults": {
        "max_positions": 8,
        "position_sizing": "score_weight",
        "entry_fill": "open_t+1",
        "exit_fill": "open_t+1",
    },
    "basic_filter": {
        "price_min": 0.1,
        "price_max": 2000,
        "market_cap_min": None,
        "float_cap_min": None,
        "amount_min": 2e7,
        "exclude_st": True,
        "exclude_new_days": 60,
        "boards": [],
    },
    "params": [
        {"id": "top_pct", "label": "每日最强比例", "type": "float", "default": 0.10,
         "min": 0.005, "max": 0.5, "step": 0.005},
        {"id": "breadth_min", "label": "趋势广度下限", "type": "float", "default": 0.50,
         "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "min_momentum_20d", "label": "20日最小动量", "type": "float", "default": 0.03,
         "min": -0.2, "max": 1.0, "step": 0.01},
        {"id": "min_momentum_60d", "label": "60日最小动量", "type": "float", "default": 0.18,
         "min": -0.2, "max": 2.0, "step": 0.01},
        {"id": "max_momentum_5d", "label": "5日最大动量", "type": "float", "default": 0.25,
         "min": 0.02, "max": 1.0, "step": 0.01},
        {"id": "max_annual_vol", "label": "最大年化波动", "type": "float", "default": 0.80,
         "min": 0.1, "max": 3.0, "step": 0.05},
        {"id": "breakout_buffer", "label": "距60日新高容差", "type": "float", "default": 0.12,
         "min": 0.0, "max": 0.3, "step": 0.01},
        {"id": "min_vol_ratio", "label": "最低量比", "type": "float", "default": 1.0,
         "min": 0.1, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"momentum_60d": 0.50, "momentum_20d": 0.30, "momentum_10d": 0.20},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS: list[str] = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.08
TRAILING_STOP = 0.06
TRAILING_TAKE_PROFIT_ACTIVATE = None
TRAILING_TAKE_PROFIT_DRAWDOWN = None
MAX_HOLD_DAYS = 10
LOOKBACK_DAYS = 120
ALERTS = []


class AdaptiveRelativeMomentumMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        top_pct = min(max(float(params.get("top_pct", 0.10)), 0.005), 0.5)
        breadth_min = min(max(float(params.get("breadth_min", 0.50)), 0.0), 1.0)
        min_mom20 = float(params.get("min_momentum_20d", 0.03))
        min_mom60 = float(params.get("min_momentum_60d", 0.18))
        max_mom5 = float(params.get("max_momentum_5d", 0.25))
        max_vol = max(float(params.get("max_annual_vol", 0.80)), 0.01)
        breakout_buffer = min(max(float(params.get("breakout_buffer", 0.12)), 0.0), 0.5)
        min_vol_ratio = max(float(params.get("min_vol_ratio", 1.0)), 0.0)

        ma20 = matrix_feature(market, "ma20")
        ma60 = matrix_feature(market, "ma60")
        high_60d = matrix_feature(market, "high_60d")
        momentum_5d = matrix_feature(market, "momentum_5d")
        momentum_10d = matrix_feature(market, "momentum_10d")
        momentum_20d = matrix_feature(market, "momentum_20d")
        momentum_60d = matrix_feature(market, "momentum_60d")
        annual_vol_20d = matrix_feature(market, "annual_vol_20d")
        vol_ratio_5d = matrix_feature(market, "vol_ratio_5d")

        eligible = (
            (market.close > ma20)
            & (ma20 > ma60)
            & (momentum_20d >= min_mom20)
            & (momentum_60d >= min_mom60)
            & (momentum_5d <= max_mom5)
            & (annual_vol_20d <= max_vol)
            & (market.close >= high_60d * (1.0 - breakout_buffer))
            & (vol_ratio_5d >= min_vol_ratio)
        )
        eligible &= np.isfinite(
            market.close + ma20 + ma60 + high_60d + momentum_5d
            + momentum_10d + momentum_20d + momentum_60d
            + annual_vol_20d + vol_ratio_5d
        )

        trend_valid = np.isfinite(market.close) & np.isfinite(ma60)
        trend_count = trend_valid.sum(axis=1)
        trend_breadth = np.divide(
            ((market.close > ma60) & trend_valid).sum(axis=1),
            trend_count,
            out=np.zeros(market.shape[0], dtype=np.float64),
            where=trend_count > 0,
        )
        strength = momentum_60d * 0.50 + momentum_20d * 0.30 + momentum_10d * 0.20
        entry = np.zeros(market.shape, dtype=bool)
        for time_id in range(market.shape[0]):
            if trend_breadth[time_id] < breadth_min:
                continue
            asset_ids = np.flatnonzero(eligible[time_id])
            if asset_ids.size == 0:
                continue
            top_count = max(1, int(np.ceil(asset_ids.size * top_pct)))
            order = np.argsort(-strength[time_id, asset_ids], kind="stable")
            entry[time_id, asset_ids[order[:top_count]]] = True

        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        score = np.nan_to_num(strength, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("adaptive_relative_momentum",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = AdaptiveRelativeMomentumMatrixStrategy()
