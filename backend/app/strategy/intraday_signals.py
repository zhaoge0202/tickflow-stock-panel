"""监控中心专用的日内分时信号评估器。

v2: 特征计算与条件求值统一走 intraday_features 特征帧 + custom_signals 的
盘中表达式编译 — 与分钟策略执行/分钟回测/回放验证同一条口径。

- 内置 4 个分时穿越信号(signal_intraday_*)由同一表达式机制生成, 列名不变,
  存量监控规则零迁移;
- 自定义盘中信号(timeframe="intraday", csgi_ 前缀)与内置信号一并评估注入。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import polars as pl

from app.market_time import CN_TZ
from app.strategy import custom_signals
from app.strategy.intraday_features import build_feature_frame

logger = logging.getLogger(__name__)

INTRADAY_SIGNAL_LABELS: dict[str, str] = {
    "signal_intraday_avg_cross_up": "分时价格上穿均价",
    "signal_intraday_avg_cross_down": "分时价格下穿均价",
    "signal_intraday_zero_cross_up": "分时价格上穿0轴",
    "signal_intraday_zero_cross_down": "分时价格下穿0轴",
}
INTRADAY_SIGNAL_FIELDS = frozenset(INTRADAY_SIGNAL_LABELS)
_LEGACY_MIN_BARS = 2  # 旧实现要求至少两根已完成 bar 才判穿越, 语义保持


def uses_intraday_signals(rule: dict) -> bool:
    """规则是否引用盘中信号列(内置 4 个或自定义 csgi_)。"""
    return any(
        (
            isinstance(c, dict)
            and c.get("op") == "truth"
            and (c.get("field") in INTRADAY_SIGNAL_FIELDS or str(c.get("field", "")).startswith(custom_signals.INTRADAY_PREFIX))
        )
        for c in rule.get("conditions", [])
    )


def _legacy_builtin_definitions() -> list[dict]:
    """内置 4 个分时穿越信号的等价定义(与 v1 逐字节同口径)。

    v1 语义: 上穿 = 前一根 bar 未满足且当前 bar 满足 —— 与
    build_intraday_expressions 的「条件上升沿」完全一致。
    """
    return [
        {"id": "signal_intraday_avg_cross_up", "timeframe": "intraday", "enabled": True,
         "conditions": [{"left": "price", "op": "cross_up", "right": "field:vwap"}],
         "min_bars": _LEGACY_MIN_BARS},
        {"id": "signal_intraday_avg_cross_down", "timeframe": "intraday", "enabled": True,
         "conditions": [{"left": "price", "op": "cross_down", "right": "field:vwap"}],
         "min_bars": _LEGACY_MIN_BARS},
        {"id": "signal_intraday_zero_cross_up", "timeframe": "intraday", "enabled": True,
         "conditions": [{"left": "pct_vs_prev_close", "op": "cross_up", "right": 0}],
         "min_bars": _LEGACY_MIN_BARS},
        {"id": "signal_intraday_zero_cross_down", "timeframe": "intraday", "enabled": True,
         "conditions": [{"left": "pct_vs_prev_close", "op": "cross_down", "right": 0}],
         "min_bars": _LEGACY_MIN_BARS},
    ]


def _naive_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(CN_TZ).replace(tzinfo=None)
    return value


class IntradaySignalEvaluator:
    """按已完成的一分钟 K 线评估盘中信号(边沿触发, 新 bar 出现才可能触发)。"""

    def __init__(self) -> None:
        self._last_bar: dict[tuple[str, str], datetime] = {}

    def evaluate(
        self,
        minute_df: pl.DataFrame,
        *,
        symbols: set[str],
        prev_close: dict[str, float],
        asset_type: str,
        now: datetime,
        signals: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """返回本分钟触发信号的行列表(每 symbol 一行, 仅新出现的 bar 触发)。"""
        active_keys = {(asset_type, symbol) for symbol in symbols}
        self._last_bar = {
            key: value for key, value in self._last_bar.items()
            if key[0] != asset_type or key in active_keys
        }
        definitions = _legacy_builtin_definitions() + list(signals or [])
        if not symbols:
            return []

        frame = build_feature_frame(
            minute_df.filter(pl.col("symbol").cast(pl.Utf8).is_in(sorted(symbols))),
            prev_close=prev_close,
            cutoff=now,
        )
        if frame.is_empty():
            return []

        exprs = custom_signals.build_intraday_expressions(definitions)
        if not exprs:
            return []
        # 内置 4 信号保留历史列名(不带 csgi_ 前缀) — 存量监控规则零迁移
        for legacy_id in INTRADAY_SIGNAL_FIELDS:
            prefixed = custom_signals.intraday_column_name(legacy_id)
            if prefixed in exprs:
                exprs[legacy_id] = exprs.pop(prefixed)
        min_bars_by_col = {
            custom_signals.intraday_column_name(d["id"]): int(d.get("min_bars", 0) or 0)
            for d in definitions
        }
        min_bars_by_col.update({
            name: _LEGACY_MIN_BARS for name in INTRADAY_SIGNAL_FIELDS
        })

        evaluated = custom_signals.apply_intraday_edges(frame, exprs)
        # min_bars 门槛: 当日已完成 bar 数不足时强制不触发
        evaluated = evaluated.with_columns(
            pl.int_range(pl.len()).over(["symbol", "date"]).alias("_bar_idx")
        )
        for name, min_bars in min_bars_by_col.items():
            if name in evaluated.columns and min_bars > 0:
                evaluated = evaluated.with_columns(
                    pl.when(pl.col("_bar_idx") + 1 >= min_bars)
                    .then(pl.col(name))
                    .otherwise(False)
                    .alias(name)
                )

        cutoff = _naive_datetime(now)
        results: list[dict[str, Any]] = []
        signal_cols = [name for name in exprs if name in evaluated.columns]
        for part in evaluated.partition_by("symbol", maintain_order=False):
            part = part.sort("datetime")
            symbol = str(part["symbol"][0])
            last_time = part["datetime"][-1]
            if cutoff is not None and last_time.date() != cutoff.date():
                continue
            key = (asset_type, symbol)
            last_seen = self._last_bar.get(key)
            self._last_bar[key] = last_time
            # 只有出现新 bar 才可能触发; 首次见到该标的只建状态不发信号
            if last_seen is None or last_time <= last_seen or last_time.date() != last_seen.date():
                continue
            row = {name: bool(part[name][-1]) for name in signal_cols}
            if any(row.values()):
                row["symbol"] = symbol
                results.append(row)
        return results

    @staticmethod
    def inject(df: pl.DataFrame, signals: list[dict[str, Any]]) -> pl.DataFrame:
        """把本分钟触发的信号以布尔列注入 enriched 快照(缺省 False)。"""
        fields = sorted(INTRADAY_SIGNAL_FIELDS | {f for s in signals for f in s if f != "symbol"})
        existing = [field for field in fields if field in df.columns]
        out = df.drop(existing) if existing else df
        if signals:
            cols = sorted({f for s in signals for f in s if f != "symbol"})
            out = out.join(pl.DataFrame(signals).select(["symbol", *cols]), on="symbol", how="left")
        out = out.with_columns([
            (
                pl.col(field).fill_null(False).cast(pl.Boolean).alias(field)
                if field in out.columns
                else pl.lit(False, dtype=pl.Boolean).alias(field)
            )
            for field in fields
        ])
        return out
