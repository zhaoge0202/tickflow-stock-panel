"""逼近涨停 — 涨幅 > 7% 且距涨停 < 3%, 盘后选股"""
from datetime import date

import numpy as np
import polars as pl

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE = date(2026, 7, 6)
_MILLISECONDS_PER_DAY = 86_400_000
_ST_MAIN_BOARD_10PCT_EFFECTIVE_TIMESTAMP = (
    ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE - date(1970, 1, 1)
).days * _MILLISECONDS_PER_DAY


def _limit_pct(date_col: str | None = None) -> pl.Expr:
    """根据板块和 ST 动态计算涨跌幅限制 (小数)。
    创业板(300/301)/科创板(688): 20% (含其 ST)
    北交所(.BJ): 30%
    主板 ST: 2026-07-06 前 5%, 之后 10%
    主板普通: 10%
    """
    is_st = pl.col("name").str.contains("(?i)ST").fill_null(False)
    is_cyb = pl.col("symbol").str.starts_with("300") | pl.col("symbol").str.starts_with("301")
    is_kcb = pl.col("symbol").str.starts_with("688")
    is_bj = pl.col("symbol").str.contains(r"\.BJ$")
    is_before_st_upgrade = (
        pl.col(date_col) < ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE
        if date_col else pl.lit(False)
    )
    return (
        pl.when(is_cyb | is_kcb).then(0.20)
        .when(is_bj).then(0.30)
        .when(is_st & is_before_st_upgrade).then(0.05)
        .otherwise(0.10)
    )

META = {
    "id": "near_limit_up",
    "name": "逼近涨停",
    "description": "涨幅 > 7% 且距涨停 < 3%, 追涨信号",
    "tags": ["涨停", "追涨"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 7.0,
            "min": 3.0,
            "max": 15.0,
            "step": 1.0,
        },
        {
            "id": "use_limit_gap_filter",
            "label": "启用距涨停空间过滤",
            "type": "bool",
            "default": True,
        },
        {
            "id": "limit_gap",
            "label": "距涨停空间%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"change_pct": 0.5, "amount": 0.3, "momentum_5d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5
ALERTS = []


class NearLimitUpMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    @staticmethod
    def _limit_pct(market: MarketDataMatrix) -> np.ndarray:
        """返回最后一个交易日各标的限幅，供诊断和兼容调用。"""
        return NearLimitUpMatrixStrategy._limit_pct_matrix(market)[-1]

    @staticmethod
    def _limit_pct_matrix(market: MarketDataMatrix) -> np.ndarray:
        values = np.full(market.shape, 0.10, dtype=np.float32)
        before_st_upgrade = market.timestamps < _ST_MAIN_BOARD_10PCT_EFFECTIVE_TIMESTAMP
        for asset_id, (symbol, name) in enumerate(zip(market.symbols, market.names, strict=True)):
            if symbol.startswith(("300", "301", "688")):
                values[:, asset_id] = 0.20
            elif symbol.endswith(".BJ"):
                values[:, asset_id] = 0.30
            elif "ST" in name.upper():
                values[:, asset_id] = np.where(before_st_upgrade, 0.05, 0.10)
        return values

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        change = matrix_feature(market, "change_pct")
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_change_filter", True):
            entry &= change > float(params.get("min_change", 7.0)) / 100.0
        if params.get("use_limit_gap_filter", True):
            entry &= (
                change
                < self._limit_pct_matrix(market) - float(params.get("limit_gap", 3.0)) / 100.0
            )
        ma20 = matrix_feature(market, "ma20")
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = NearLimitUpMatrixStrategy()
