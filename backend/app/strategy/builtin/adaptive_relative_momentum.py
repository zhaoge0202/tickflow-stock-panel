"""截面动量加速：市场状态过滤后，轮动到绝对动量最强的少数标的。"""
from __future__ import annotations

import polars as pl

META = {
    "id": "adaptive_relative_momentum",
    "name": "截面动量加速",
    "description": "趋势广度择时 + 20/60日绝对动量 + 截面相对强度轮动，股票与ETF通用",
    "tags": ["动量", "相对强弱", "趋势", "股票ETF"],
    "asset_types": ["stock", "etf"],
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

ENTRY_SIGNALS: list[str] = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.08
TRAILING_STOP = 0.06
TRAILING_TAKE_PROFIT_ACTIVATE = None
TRAILING_TAKE_PROFIT_DRAWDOWN = None
MAX_HOLD_DAYS = 10
LOOKBACK_DAYS = 120
ALERTS = []


_REQUIRED_COLUMNS = {
    "symbol", "date", "close", "ma20", "ma60", "high_60d",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_60d",
    "annual_vol_20d", "vol_ratio_5d",
}


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """只使用当日及历史指标，逐日保留满足趋势条件的截面最强标的。"""
    if df.is_empty() or not _REQUIRED_COLUMNS.issubset(df.columns):
        return df.head(0)

    top_pct = min(max(float(params.get("top_pct", 0.10)), 0.005), 0.5)
    breadth_min = min(max(float(params.get("breadth_min", 0.50)), 0.0), 1.0)
    min_mom20 = float(params.get("min_momentum_20d", 0.03))
    min_mom60 = float(params.get("min_momentum_60d", 0.18))
    max_mom5 = float(params.get("max_momentum_5d", 0.25))
    max_vol = max(float(params.get("max_annual_vol", 0.80)), 0.01)
    breakout_buffer = min(max(float(params.get("breakout_buffer", 0.12)), 0.0), 0.5)
    min_vol_ratio = max(float(params.get("min_vol_ratio", 1.0)), 0.0)

    eligible = (
        (pl.col("close") > pl.col("ma20"))
        & (pl.col("ma20") > pl.col("ma60"))
        & (pl.col("momentum_20d") >= min_mom20)
        & (pl.col("momentum_60d") >= min_mom60)
        & (pl.col("momentum_5d") <= max_mom5)
        & (pl.col("annual_vol_20d") <= max_vol)
        & (pl.col("close") >= pl.col("high_60d") * (1 - breakout_buffer))
        & (pl.col("vol_ratio_5d") >= min_vol_ratio)
    ).fill_null(False)

    strength = (
        pl.col("momentum_60d") * 0.50
        + pl.col("momentum_20d") * 0.30
        + pl.col("momentum_10d") * 0.20
    )
    work = df.with_columns([
        eligible.alias("_eligible"),
        (pl.col("close") > pl.col("ma60"))
        .fill_null(False)
        .cast(pl.Float64)
        .mean()
        .over("date")
        .alias("_trend_breadth"),
    ]).with_columns([
        pl.when(pl.col("_eligible"))
        .then(strength)
        .otherwise(None)
        .rank(method="ordinal", descending=True)
        .over("date")
        .alias("_strength_rank"),
        pl.col("_eligible").sum().over("date").alias("_eligible_count"),
    ])

    top_count = pl.max_horizontal(
        pl.lit(1.0),
        (pl.col("_eligible_count") * top_pct).ceil(),
    )
    return work.filter(
        pl.col("_eligible")
        & (pl.col("_trend_breadth") >= breadth_min)
        & (pl.col("_strength_rank") <= top_count)
    ).drop("_eligible", "_trend_breadth", "_strength_rank", "_eligible_count")
