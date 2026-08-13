"""策略评分字段解析。"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import polars as pl

SCORING_DIRECTION_HIGH = "high"
SCORING_DIRECTION_LOW = "low"
SCORING_DIRECTIONS = frozenset({SCORING_DIRECTION_HIGH, SCORING_DIRECTION_LOW})

VIRTUAL_SCORING_DEPENDENCIES: dict[str, frozenset[str]] = {
    **{
        f"ma{period}_bias": frozenset({"close", f"ma{period}"})
        for period in (5, 10, 20, 30, 60)
    },
    **{
        f"ema{period}_bias": frozenset({"close", f"ema{period}"})
        for period in (5, 10, 20, 30, 60)
    },
    "macd_dif_pct": frozenset({"close", "macd_dif"}),
    "macd_dea_pct": frozenset({"close", "macd_dea"}),
    "macd_hist_pct": frozenset({"close", "macd_hist"}),
    "boll_position": frozenset({"close", "boll_upper", "boll_lower"}),
    "atr_pct": frozenset({"close", "atr_14"}),
    "boll_width": frozenset({"ma20", "boll_upper", "boll_lower"}),
    "vol_ratio_10d": frozenset({"volume"}),
    "vol_trend_5_10": frozenset({"vol_ma5", "vol_ma10"}),
    "turnover_ratio_5d": frozenset({"turnover_rate"}),
    "log_amount": frozenset({"amount"}),
    "amount_ratio_5d": frozenset({"amount"}),
    "gap_return": frozenset({"open", "prev_close"}),
    "intraday_return": frozenset({"open", "close"}),
    "close_position": frozenset({"high", "low", "close"}),
    "distance_to_high_60d": frozenset({"close", "high_60d"}),
    "distance_from_low_60d": frozenset({"close", "low_60d"}),
}

_ROLLING_SCORING_WARMUP: dict[str, int] = {
    "vol_ratio_10d": 11,
    "turnover_ratio_5d": 6,
    "amount_ratio_5d": 6,
}


def effective_scoring(
    defaults: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """解析有效评分；新配置可完整替换，历史配置保持局部覆盖。"""
    override_values = (overrides or {}).get("scoring")
    if (overrides or {}).get("scoring_replace") is True:
        return dict(override_values) if isinstance(override_values, Mapping) else {}
    scoring = dict(defaults or {})
    if isinstance(override_values, Mapping):
        scoring.update(override_values)
    return scoring


def effective_scoring_directions(overrides: Mapping[str, Any] | None) -> dict[str, str]:
    values = (overrides or {}).get("scoring_directions")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): str(direction)
        for name, direction in values.items()
        if direction in SCORING_DIRECTIONS
    }


def scoring_warmup_bars(scoring: Mapping[str, Any]) -> int:
    return max(
        (_ROLLING_SCORING_WARMUP.get(str(name), 1) for name, weight in scoring.items() if weight),
        default=1,
    )


def scoring_dependencies(scoring: Mapping[str, Any]) -> set[str]:
    """把受控虚拟评分字段展开为实际数据依赖。"""
    dependencies: set[str] = set()
    for name, weight in scoring.items():
        if not weight:
            continue
        dependencies.update(VIRTUAL_SCORING_DEPENDENCIES.get(str(name), {str(name)}))
    return dependencies


def scoring_value_expr(columns: Collection[str], name: str) -> pl.Expr | None:
    """返回评分值表达式；依赖不完整时返回 None。"""
    available = set(columns)
    if name in available:
        return pl.col(name)
    dependencies = VIRTUAL_SCORING_DEPENDENCIES.get(name)
    if dependencies is None or not dependencies.issubset(available):
        return None
    if name.startswith("ma") and name.endswith("_bias"):
        period = name.removeprefix("ma").removesuffix("_bias")
        if period.isdigit():
            return _relative(pl.col("close"), pl.col(f"ma{period}"))
    if name.startswith("ema") and name.endswith("_bias"):
        period = name.removeprefix("ema").removesuffix("_bias")
        if period.isdigit():
            return _relative(pl.col("close"), pl.col(f"ema{period}"))
    if name in {"macd_dif_pct", "macd_dea_pct", "macd_hist_pct"}:
        source = name.removesuffix("_pct")
        return _ratio(pl.col(source), pl.col("close"))
    if name == "atr_pct":
        return _ratio(pl.col("atr_14"), pl.col("close"))
    if name == "boll_position":
        return _ratio(
            pl.col("close") - pl.col("boll_lower"),
            pl.col("boll_upper") - pl.col("boll_lower"),
        )
    if name == "boll_width":
        return _ratio(pl.col("boll_upper") - pl.col("boll_lower"), pl.col("ma20"))
    if name == "vol_ratio_10d":
        return _ratio(
            pl.col("volume"),
            pl.col("volume").shift(1).rolling_mean(10).over("symbol"),
        )
    if name == "vol_trend_5_10":
        return _relative(pl.col("vol_ma5"), pl.col("vol_ma10"))
    if name == "turnover_ratio_5d":
        return _relative(
            pl.col("turnover_rate"),
            pl.col("turnover_rate").shift(1).rolling_mean(5).over("symbol"),
        )
    if name == "log_amount":
        return pl.when(pl.col("amount") >= 0).then((pl.col("amount") + 1).log()).otherwise(None)
    if name == "amount_ratio_5d":
        return _relative(
            pl.col("amount"),
            pl.col("amount").shift(1).rolling_mean(5).over("symbol"),
        )
    if name == "gap_return":
        return _relative(pl.col("open"), pl.col("prev_close"))
    if name == "intraday_return":
        return _relative(pl.col("close"), pl.col("open"))
    if name == "close_position":
        return _ratio(pl.col("close") - pl.col("low"), pl.col("high") - pl.col("low"))
    if name == "distance_to_high_60d":
        return _relative(pl.col("close"), pl.col("high_60d"))
    if name == "distance_from_low_60d":
        return _relative(pl.col("close"), pl.col("low_60d"))
    return None


def materialize_scoring_columns(
    frame: pl.DataFrame,
    names: Collection[str],
) -> pl.DataFrame:
    expressions = [
        expression.alias(name)
        for name in names
        if name not in frame.columns
        and (expression := scoring_value_expr(frame.columns, str(name))) is not None
    ]
    return frame.with_columns(expressions) if expressions else frame


def _ratio(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return pl.when(denominator.is_not_null() & (denominator != 0)).then(
        numerator / denominator
    ).otherwise(None)


def _relative(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return _ratio(numerator, denominator) - 1.0
