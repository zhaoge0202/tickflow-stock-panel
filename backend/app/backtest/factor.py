"""因子回测服务 — IC/IR 分析 + 分层回测 + 多空组合。

纯 Polars 向量化实现，无 pandas 依赖。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine
from app.strategy.scoring import (
    VIRTUAL_SCORING_DEPENDENCIES as DERIVED_FACTOR_DEPENDENCIES,
)
from app.strategy.scoring import (
    materialize_scoring_columns,
)

logger = logging.getLogger(__name__)

# 可研究因子目录。保留历史 ID 兼容已有候选方案; 价格尺度相关指标优先提供归一化版本。
FACTOR_COLUMNS: list[dict] = [
    {"id": "momentum_5d", "label": "5日动量", "group": "动量", "desc": "5个交易日累计收益率"},
    {"id": "momentum_10d", "label": "10日动量", "group": "动量", "desc": "10个交易日累计收益率"},
    {"id": "momentum_20d", "label": "20日动量", "group": "动量", "desc": "20个交易日累计收益率"},
    {"id": "momentum_30d", "label": "30日动量", "group": "动量", "desc": "30个交易日累计收益率"},
    {"id": "momentum_60d", "label": "60日动量", "group": "动量", "desc": "60个交易日累计收益率"},
    {"id": "change_pct", "label": "日涨跌幅", "group": "动量", "desc": "当日收盘相对前收盘的收益率"},

    {"id": "ma5_bias", "label": "MA5乖离", "group": "均线偏离", "desc": "收盘价 / MA5 - 1"},
    {"id": "ma10_bias", "label": "MA10乖离", "group": "均线偏离", "desc": "收盘价 / MA10 - 1"},
    {"id": "ma20_bias", "label": "MA20乖离", "group": "均线偏离", "desc": "收盘价 / MA20 - 1"},
    {"id": "ma30_bias", "label": "MA30乖离", "group": "均线偏离", "desc": "收盘价 / MA30 - 1"},
    {"id": "ma60_bias", "label": "MA60乖离", "group": "均线偏离", "desc": "收盘价 / MA60 - 1"},
    {"id": "ema5_bias", "label": "EMA5乖离", "group": "均线偏离", "desc": "收盘价 / EMA5 - 1"},
    {"id": "ema10_bias", "label": "EMA10乖离", "group": "均线偏离", "desc": "收盘价 / EMA10 - 1"},
    {"id": "ema20_bias", "label": "EMA20乖离", "group": "均线偏离", "desc": "收盘价 / EMA20 - 1"},
    {"id": "ema30_bias", "label": "EMA30乖离", "group": "均线偏离", "desc": "收盘价 / EMA30 - 1"},
    {"id": "ema60_bias", "label": "EMA60乖离", "group": "均线偏离", "desc": "收盘价 / EMA60 - 1"},

    {"id": "rsi_6", "label": "RSI(6)", "group": "超买超卖", "desc": "6日相对强弱指标"},
    {"id": "rsi_14", "label": "RSI(14)", "group": "超买超卖", "desc": "14日相对强弱指标"},
    {"id": "rsi_24", "label": "RSI(24)", "group": "超买超卖", "desc": "24日相对强弱指标"},

    {"id": "macd_hist", "label": "MACD柱(原值)", "group": "趋势", "desc": "兼容历史研究; 跨股票比较建议优先使用MACD柱强度"},
    {"id": "macd_dif_pct", "label": "MACD DIF强度", "group": "趋势", "desc": "MACD DIF / 收盘价"},
    {"id": "macd_dea_pct", "label": "MACD DEA强度", "group": "趋势", "desc": "MACD DEA / 收盘价"},
    {"id": "macd_hist_pct", "label": "MACD柱强度", "group": "趋势", "desc": "MACD柱 / 收盘价, 消除股价尺度影响"},
    {"id": "kdj_k", "label": "KDJ-K", "group": "趋势", "desc": "KDJ指标K值"},
    {"id": "kdj_d", "label": "KDJ-D", "group": "趋势", "desc": "KDJ指标D值"},
    {"id": "kdj_j", "label": "KDJ-J", "group": "趋势", "desc": "KDJ指标J值"},
    {"id": "boll_position", "label": "布林位置", "group": "趋势", "desc": "收盘价在布林带下轨到上轨之间的位置"},

    {"id": "annual_vol_20d", "label": "20日波动率", "group": "波动率", "desc": "20日收益率年化标准差"},
    {"id": "atr_14", "label": "ATR(14)原值", "group": "波动率", "desc": "兼容历史研究; 跨股票比较建议优先使用ATR相对波动"},
    {"id": "atr_pct", "label": "ATR相对波动", "group": "波动率", "desc": "ATR(14) / 收盘价"},
    {"id": "amplitude", "label": "日振幅", "group": "波动率", "desc": "当日高低价差 / 前收盘价"},
    {"id": "boll_width", "label": "布林带宽", "group": "波动率", "desc": "布林带上下轨宽度 / MA20"},

    {"id": "vol_ratio_5d", "label": "5日量比", "group": "量价", "desc": "当日成交量 / 前5日平均成交量"},
    {"id": "vol_ratio_10d", "label": "10日量比", "group": "量价", "desc": "当日成交量 / 前10日平均成交量"},
    {"id": "vol_trend_5_10", "label": "成交量趋势", "group": "量价", "desc": "5日平均成交量 / 10日平均成交量 - 1"},
    {"id": "turnover_rate", "label": "换手率", "group": "量价", "desc": "使用历史时点流通股本计算的当日换手率"},
    {"id": "turnover_ratio_5d", "label": "换手率放大", "group": "量价", "desc": "当日换手率 / 前5日平均换手率 - 1"},
    {"id": "log_amount", "label": "成交额对数", "group": "量价", "desc": "ln(成交额 + 1), 降低极端规模影响"},
    {"id": "amount_ratio_5d", "label": "成交额放大", "group": "量价", "desc": "当日成交额 / 前5日平均成交额 - 1"},

    {"id": "gap_return", "label": "开盘跳空", "group": "价格位置", "desc": "开盘价 / 前收盘价 - 1"},
    {"id": "intraday_return", "label": "日内收益", "group": "价格位置", "desc": "收盘价 / 开盘价 - 1"},
    {"id": "close_position", "label": "收盘位置", "group": "价格位置", "desc": "收盘价在当日最低价到最高价之间的位置"},
    {"id": "distance_to_high_60d", "label": "距60日高点", "group": "价格位置", "desc": "收盘价 / 60日最高收盘价 - 1"},
    {"id": "distance_from_low_60d", "label": "距60日低点", "group": "价格位置", "desc": "收盘价 / 60日最低收盘价 - 1"},
]

FACTOR_WARMUP_DAYS = 120


@dataclass
class FactorConfig:
    factor_name: str
    symbols: list[str] | None
    start: date
    end: date
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"


@dataclass
class GroupStats:
    group: int
    label: str
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float


@dataclass
class FactorResult:
    run_id: str
    config: dict
    # IC 分析
    ic_mean: float | None = None
    ic_std: float | None = None
    ir: float | None = None
    ic_win_rate: float | None = None
    ic_series: list[dict] = field(default_factory=list)
    # 分层
    group_stats: list[dict] = field(default_factory=list)
    group_nav: list[dict] = field(default_factory=list)
    # 多空
    long_short_stats: dict = field(default_factory=dict)
    long_short_nav: list[dict] = field(default_factory=list)
    # 元信息
    elapsed_ms: float = 0.0
    n_symbols: int = 0
    n_dates: int = 0
    error: str | None = None


@dataclass
class FactorBatchConfig:
    factor_names: list[str]
    symbols: list[str] | None
    start: date
    end: date
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"


@dataclass
class FactorBatchItem:
    factor_name: str
    label: str
    group: str
    ic_mean: float | None = None
    ir: float | None = None
    ic_win_rate: float | None = None
    long_short_return: float | None = None
    long_short_max_drawdown: float | None = None
    n_symbols: int = 0
    n_dates: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None


@dataclass
class FactorBatchResult:
    run_id: str
    config: dict
    results: list[FactorBatchItem] = field(default_factory=list)
    elapsed_ms: float = 0.0
    n_symbols: int = 0
    n_dates: int = 0
    error: str | None = None


class FactorBacktestService:
    def __init__(self, engine: BacktestEngine) -> None:
        self.engine = engine

    def run(self, config: FactorConfig) -> FactorResult:
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]
        panel = self._load_factor_panel(config, [config.factor_name])
        if panel.is_empty():
            return self._error_result(config, run_id, t0, "无数据, 请检查日期范围或先运行盘后管道")

        return self._evaluate_panel(panel, config, run_id, t0)

    def run_batch(self, config: FactorBatchConfig) -> FactorBatchResult:
        """在同一份 Panel 上依次评估多个因子, 避免重复读取和计算指标。"""
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]
        factor_names = list(dict.fromkeys(config.factor_names))
        result_config = self._batch_config_to_dict(config, factor_names)
        if not factor_names:
            return FactorBatchResult(
                run_id=run_id,
                config=result_config,
                error="至少选择一个因子",
            )

        panel = self._load_factor_panel(config, factor_names)
        if panel.is_empty():
            return FactorBatchResult(
                run_id=run_id,
                config=result_config,
                error="无数据, 请检查日期范围或先运行盘后管道",
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        # P1: 预计算共享下期收益 (仅依赖 close/date/symbol), 避免每个因子重复 shift/调仓日 JOIN。
        panel = self._attach_shared_next_return(panel, config)

        metadata = {item["id"]: item for item in FACTOR_COLUMNS}
        items: list[FactorBatchItem] = []
        for factor_name in factor_names:
            item_t0 = time.perf_counter()
            factor_config = FactorConfig(
                factor_name=factor_name,
                symbols=config.symbols,
                start=config.start,
                end=config.end,
                n_groups=config.n_groups,
                rebalance=config.rebalance,
                weight=config.weight,
                fees_pct=config.fees_pct,
                slippage_bps=config.slippage_bps,
                asset_type=config.asset_type,
            )
            meta = metadata.get(factor_name, {})
            try:
                result = self._evaluate_panel(
                    panel,
                    factor_config,
                    f"{run_id}-{len(items) + 1}",
                    item_t0,
                )
                long_short = result.long_short_stats
                items.append(FactorBatchItem(
                    factor_name=factor_name,
                    label=str(meta.get("label", factor_name)),
                    group=str(meta.get("group", "")),
                    ic_mean=result.ic_mean,
                    ir=result.ir,
                    ic_win_rate=result.ic_win_rate,
                    long_short_return=long_short.get("total_return"),
                    long_short_max_drawdown=long_short.get("max_drawdown"),
                    n_symbols=result.n_symbols,
                    n_dates=result.n_dates,
                    elapsed_ms=result.elapsed_ms,
                    error=result.error,
                ))
            except Exception as exc:  # 单因子失败不能中止整个筛选批次
                logger.exception("factor batch item failed: %s", factor_name)
                items.append(FactorBatchItem(
                    factor_name=factor_name,
                    label=str(meta.get("label", factor_name)),
                    group=str(meta.get("group", "")),
                    elapsed_ms=round((time.perf_counter() - item_t0) * 1000, 1),
                    error=str(exc),
                ))

        n_symbols = max((item.n_symbols for item in items), default=0)
        n_dates = max((item.n_dates for item in items), default=0)
        return FactorBatchResult(
            run_id=run_id,
            config=result_config,
            results=items,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            n_symbols=n_symbols,
            n_dates=n_dates,
        )

    def _load_factor_panel(
        self,
        config: FactorConfig | FactorBatchConfig,
        factor_names: list[str],
    ) -> pl.DataFrame:
        panel_columns = [
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "turnover_rate",
        ]
        panel_columns.extend(name for name in factor_names if name not in panel_columns)
        load_start = config.start
        if any(name != "turnover_rate" for name in factor_names):
            load_start = config.start - timedelta(days=FACTOR_WARMUP_DAYS)

        panel = self.engine.load_panel(
            config.symbols,
            load_start,
            config.end,
            columns=panel_columns,
            asset_type=config.asset_type,
        )
        if panel.is_empty():
            return panel

        missing = set(factor_names) - set(panel.columns)
        if missing:
            panel = self._compute_missing_factors(panel, missing)
        return panel

    @staticmethod
    def _attach_shared_next_return(
        panel: pl.DataFrame, config: FactorBatchConfig,
    ) -> pl.DataFrame:
        """对 [start, end] 内 close 有效序列计算一次 _next_return, 复用给批次内每个因子。

        _next_return 仅依赖 (symbol, date, close, rebalance), 与具体因子无关; 因子空值
        集中在预热期前缀, 过滤后剩余序列无内部空洞, 故与 _evaluate_panel 内逐因子在
        过滤后面板上重算的结果完全等价。
        """
        if "_next_return" in panel.columns:
            return panel
        base = (
            panel.filter((pl.col("date") >= config.start) & (pl.col("date") <= config.end))
            .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
            .select(["symbol", "date", "close"])
            .sort(["symbol", "date"])
        )
        if base.is_empty():
            return panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
        if config.rebalance == "daily":
            base = base.with_columns(
                (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1)
                .alias("_next_return")
            )
        else:
            base = FactorBacktestService._calc_period_return(base, config.rebalance)
        return panel.join(
            base.select(["symbol", "date", "_next_return"]),
            on=["symbol", "date"],
            how="left",
        )

    def _evaluate_panel(
        self,
        source_panel: pl.DataFrame,
        config: FactorConfig,
        run_id: str,
        t0: float,
    ) -> FactorResult:
        def _err(msg: str) -> FactorResult:
            return self._error_result(config, run_id, t0, msg)

        factor_col = config.factor_name
        if factor_col not in source_panel.columns:
            return _err(f"因子列 '{factor_col}' 不存在于 enriched 数据中, 且无法从基础行情计算")
        if "close" not in source_panel.columns:
            return _err("enriched 数据缺少收盘价 close")

        # 批量模式由 run_batch 预计算 _next_return 并随 source_panel 传入, 直接复用;
        # 单因子 run() 路径未预计算, 仍按原逻辑在此计算。
        select_cols = ["symbol", "date", "close", factor_col]
        precomputed_return = "_next_return" in source_panel.columns
        if precomputed_return:
            select_cols.append("_next_return")
        panel = source_panel.select(select_cols)
        panel = panel.filter((pl.col("date") >= config.start) & (pl.col("date") <= config.end))

        # 过滤有效行
        panel = panel.filter(
            pl.col(factor_col).is_not_null()
            & pl.col("close").is_not_null()
            & (pl.col("close") > 0)
        )
        if panel.is_empty():
            return _err("过滤后无有效数据")

        panel = panel.sort(["symbol", "date"])

        n_symbols = panel["symbol"].n_unique()
        n_dates = panel["date"].n_unique()

        # 计算下期收益 — _next_return 仅依赖 (symbol, date, close, rebalance), 与因子无关;
        # 因子空值集中在预热期前缀, 过滤后剩余序列无内部空洞, 故预计算与逐因子重算结果等价。
        if not precomputed_return:
            if config.rebalance == "daily":
                panel = panel.with_columns(
                    (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1)
                    .alias("_next_return")
                )
            else:
                # weekly/monthly: 计算到下个调仓日的收益
                panel = self._calc_period_return(panel, config.rebalance)

        # ── 1. IC 分析 ──
        ic_df = self._calc_ic(panel, factor_col)
        ic_series = [
            {"date": str(row["date"]), "ic": round(float(row["ic"]), 4)}
            for row in ic_df.iter_rows(named=True)
            if row["ic"] is not None and not np.isnan(float(row["ic"]))
        ]
        ic_values = [r["ic"] for r in ic_series]
        ic_mean = float(np.mean(ic_values)) if ic_values else None
        ic_std = float(np.std(ic_values)) if ic_values else None
        ir = (ic_mean / ic_std) if (ic_mean is not None and ic_std and ic_std > 1e-8) else None
        ic_win_rate = (sum(1 for v in ic_values if v > 0) / len(ic_values)) if ic_values else None

        # ── 2. 分层回测 ──
        panel = self._add_groups(panel, factor_col, config.n_groups)
        group_nav = self._calc_group_nav(panel, config)
        group_stats = self._calc_group_stats(group_nav, config.start, config.end, config.rebalance)

        # ── 3. 多空组合 ──
        long_short_nav, long_short_stats = self._calc_long_short(group_nav, config)

        elapsed = (time.perf_counter() - t0) * 1000
        return FactorResult(
            run_id=run_id,
            config=self._config_to_dict(config),
            ic_mean=round(ic_mean, 4) if ic_mean is not None else None,
            ic_std=round(ic_std, 4) if ic_std is not None else None,
            ir=round(ir, 4) if ir is not None else None,
            ic_win_rate=round(ic_win_rate, 4) if ic_win_rate is not None else None,
            ic_series=ic_series,
            group_stats=group_stats,
            group_nav=group_nav,
            long_short_stats=long_short_stats,
            long_short_nav=long_short_nav,
            elapsed_ms=round(elapsed, 1),
            n_symbols=n_symbols,
            n_dates=n_dates,
        )

    @staticmethod
    def _compute_missing_factors(panel: pl.DataFrame, factor_cols: set[str]) -> pl.DataFrame:
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        if not required.issubset(panel.columns):
            missing = sorted(required - set(panel.columns))
            logger.warning("factors %s cannot be computed, missing columns: %s", factor_cols, missing)
            return panel

        from app.indicators.pipeline import compute_indicators

        derived = factor_cols & set(DERIVED_FACTOR_DEPENDENCIES)
        indicator_columns = factor_cols - derived
        for factor_name in derived:
            indicator_columns.update(DERIVED_FACTOR_DEPENDENCIES[factor_name])
        panel = compute_indicators(panel, needed=indicator_columns)
        return FactorBacktestService._compute_derived_factors(panel, derived)

    @staticmethod
    def _compute_derived_factors(panel: pl.DataFrame, factor_cols: set[str]) -> pl.DataFrame:
        return materialize_scoring_columns(panel, factor_cols)

    @staticmethod
    def _error_result(
        config: FactorConfig,
        run_id: str,
        started_at: float,
        message: str,
    ) -> FactorResult:
        return FactorResult(
            run_id=run_id,
            config=FactorBacktestService._config_to_dict(config),
            error=message,
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )

    # ── IC 计算 ──

    @staticmethod
    def _calc_ic(panel: pl.DataFrame, factor_col: str) -> pl.DataFrame:
        """计算截面 Rank IC (因子值 rank vs 下期收益 rank 的相关系数)。"""
        return (
            panel.filter(pl.col("_next_return").is_not_null())
            .group_by("date")
            .agg(
                pl.corr(
                    pl.col(factor_col).rank(method="average"),
                    pl.col("_next_return").rank(method="average"),
                ).alias("ic")
            )
            .sort("date")
        )

    # ── 调仓期收益 ──

    @staticmethod
    def _calc_period_return(panel: pl.DataFrame, rebalance: str) -> pl.DataFrame:
        """计算到下个调仓日的收益。

        weekly: 下个周调仓日 close / 今日 close - 1
        monthly: 下个月调仓日 close / 今日 close - 1
        只在调仓日标记行有效，其他行为 null。
        """
        import datetime as _dt

        all_dates = sorted(panel["date"].unique().to_list())

        if rebalance == "weekly":
            # 调仓日 = 每周一
            rebalance_dates = set()
            for d in all_dates:
                if hasattr(d, "weekday"):
                    wd = d.weekday()
                else:
                    wd = _dt.date.fromisoformat(str(d)).weekday()
                if wd == 0:  # Monday
                    rebalance_dates.add(d)
        else:  # monthly
            # 调仓日 = 每月首个交易日
            seen_months: set[str] = set()
            rebalance_dates = set()
            for d in sorted(all_dates):
                m = str(d)[:7]  # "YYYY-MM"
                if m not in seen_months:
                    seen_months.add(m)
                    rebalance_dates.add(d)

        if not rebalance_dates:
            panel = panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
            return panel

        # 对每个调仓日，找到下一个调仓日 (仅在 unique 日期上做, 成本极低)
        sorted_rebalance = sorted(rebalance_dates)
        reb_dates: list = []
        next_dates: list = []
        for i, d in enumerate(sorted_rebalance):
            if i + 1 < len(sorted_rebalance):
                reb_dates.append(d)
                next_dates.append(sorted_rebalance[i + 1])
            # 最后一个调仓日没有下一个，不计算收益

        if not reb_dates:
            panel = panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
            return panel

        panel = panel.sort(["symbol", "date"])
        date_dtype = panel.schema["date"]

        # 调仓日 → 下一调仓日 的映射表 (向量化 JOIN, 替代 Python 逐行 price_map 循环)
        rebal_df = pl.DataFrame(
            {"date": reb_dates, "_next_reb_date": next_dates}
        ).with_columns(
            pl.col("date").cast(date_dtype),
            pl.col("_next_reb_date").cast(date_dtype),
        )

        # (symbol, 下一调仓日) → 该日 close 的查找表 (等价于原 price_map, 重复取 last)
        price_lookup = (
            panel.select(
                pl.col("symbol"),
                pl.col("date").alias("_next_reb_date"),
                pl.col("close").alias("_next_close"),
            )
            .unique(subset=["symbol", "_next_reb_date"], keep="last")
        )

        # 只在调仓日标记行有效: 下一调仓日该股 close / 当日 close - 1; 缺价或非调仓日为 null
        panel = (
            panel.join(rebal_df, on="date", how="left")
            .join(price_lookup, on=["symbol", "_next_reb_date"], how="left")
            .with_columns(
                pl.when(
                    pl.col("_next_reb_date").is_not_null()
                    & pl.col("_next_close").is_not_null()
                    & (pl.col("close") > 0)
                )
                .then(pl.col("_next_close") / pl.col("close") - 1.0)
                .otherwise(None)
                .cast(pl.Float64)
                .alias("_next_return")
            )
            .drop(["_next_reb_date", "_next_close"])
            .sort(["symbol", "date"])
        )
        return panel

    # ── 分组 ──

    @staticmethod
    def _add_groups(panel: pl.DataFrame, factor_col: str, n_groups: int) -> pl.DataFrame:
        """截面序号分桶，避免 qcut 在重复因子值截面上抛错。"""
        return (
            panel.sort(["date", factor_col, "symbol"])
            .with_columns(
                (pl.cum_count("symbol").over("date") - 1).alias("_factor_ord"),
                pl.len().over("date").alias("_factor_count"),
            )
            .with_columns(
                (
                    pl.lit("Q")
                    + (
                        ((pl.col("_factor_ord") * n_groups) / pl.col("_factor_count"))
                        .floor()
                        .cast(pl.Int64)
                        + 1
                    )
                    .clip(1, n_groups)
                    .cast(pl.Utf8)
                )
                .alias("_group")
            )
            .drop(["_factor_ord", "_factor_count"])
        )

    @staticmethod
    def _group_sort_key(group: str) -> int:
        if group.startswith("Q"):
            try:
                return int(group[1:])
            except ValueError:
                pass
        return 0

    # ── 分组净值 ──

    @staticmethod
    def _calc_group_nav(panel: pl.DataFrame, config: FactorConfig) -> list[dict]:
        """计算分组净值曲线 — 只在调仓日更新净值。"""
        # 只保留有下期收益的行 (= 调仓日)
        group_ret = (
            panel.filter(pl.col("_next_return").is_not_null() & pl.col("_group").is_not_null())
            .group_by(["date", "_group"])
            .agg(pl.col("_next_return").mean().alias("group_return"))
        )

        # pivot: date × group
        pivot = group_ret.pivot(index="date", columns="_group", values="group_return").sort("date")

        if pivot.is_empty():
            return []

        group_cols = sorted([c for c in pivot.columns if c != "date"], key=FactorBacktestService._group_sort_key)

        # 向量化累乘净值: null 视为 0 收益 (净值不变); 累乘保持全精度, 输出时 round(4),
        # 等价于原 dict 累乘 `nav_values[c] *= (1+ret); entry[c] = round(nav_values[c], 4)`。
        nav_df = pivot.with_columns(
            [(1.0 + pl.col(c).fill_null(0.0)).cum_prod().alias(c) for c in group_cols]
        )
        result: list[dict] = []
        for row in nav_df.iter_rows(named=True):
            entry: dict = {"date": str(row["date"])[:10]}
            for c in group_cols:
                entry[c] = round(float(row[c]), 4)
            result.append(entry)

        return result

    # ── 分组统计 ──

    @staticmethod
    def _calc_group_stats(
        group_nav: list[dict], start: date, end: date,
        rebalance: str = "monthly",
    ) -> list[dict]:
        if not group_nav:
            return []

        group_cols = sorted(
            [k for k in group_nav[0] if k != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        n_days = max((end - start).days, 1)
        years = n_days / 365.25
        # 夏普 — 年化系数必须匹配 group_nav 的调仓频率 (每个净值点 = 一个调仓周期收益);
        # 周/月频收益若乘 √252 会把 Sharpe 高估 √(252/期数) 倍 (月频 ≈4.6x, 周频 ≈2.2x)。
        _ann = {"daily": 252, "weekly": 52, "monthly": 12}.get(rebalance, 252)

        stats = []
        for i, c in enumerate(group_cols):
            values = [r[c] for r in group_nav if r.get(c) is not None]
            if not values:
                continue
            arr = np.asarray(values, dtype=np.float64)
            last = float(arr[-1])
            total_return = last - 1.0
            annual_return = last ** (1 / max(years, 0.01)) - 1 if last > 0 else 0.0

            # 最大回撤 (向量化): 峰值 = max(1.0, 历史最高), 与原 peak 初值 1.0 的逐行 max 一致
            peak = np.maximum(np.maximum.accumulate(arr), 1.0)
            max_dd = float(np.min((arr - peak) / peak))

            # 周期收益序列 (向量化): nav[t]/nav[t-1] - 1, 仅保留 nav[t-1] > 0 的样本
            prev = arr[:-1]
            with np.errstate(divide="ignore", invalid="ignore"):
                rets = arr[1:] / prev - 1.0
            rets = rets[prev > 0]
            if rets.size:
                std = float(np.std(rets))
                sharpe = float(np.mean(rets) / std) * np.sqrt(_ann) if std > 0 else 0.0
                win_rate = float(np.mean(rets > 0))
            else:
                sharpe = 0.0
                win_rate = 0.0

            stats.append({
                "group": i + 1,
                "label": c,
                "total_return": round(total_return, 4),
                "annual_return": round(annual_return, 4),
                "max_drawdown": round(max_dd, 4),
                "sharpe": round(sharpe, 2),
                "win_rate": round(win_rate, 4),
            })

        return stats

    # ── 多空组合 ──

    @staticmethod
    def _calc_long_short(
        group_nav: list[dict], config: FactorConfig,
    ) -> tuple[list[dict], dict]:
        """多空组合: 做多最高组 + 做空最低组。"""
        if not group_nav:
            return [], {}

        group_cols = sorted(
            [k for k in group_nav[0] if k != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        if len(group_cols) < 2:
            return [], {}

        top_col = group_cols[-1]  # Q5 (最高)
        bottom_col = group_cols[0]  # Q1 (最低)

        # 向量化: 各组净值 (null 视为 1.0), 前置 1.0 作为初值 prev_top/prev_bot,
        # 等价于原逐行 prev_top/prev_bot 初始 1.0 的累乘逻辑。
        top = np.array(
            [r[top_col] if r.get(top_col) is not None else 1.0 for r in group_nav],
            dtype=np.float64,
        )
        bot = np.array(
            [r[bottom_col] if r.get(bottom_col) is not None else 1.0 for r in group_nav],
            dtype=np.float64,
        )
        prev_top = np.concatenate(([1.0], top[:-1]))
        prev_bot = np.concatenate(([1.0], bot[:-1]))

        # 分组收益; prev <= 0 时按原逻辑置 0 (做多 top, 做空 bottom = 取反)
        with np.errstate(divide="ignore", invalid="ignore"):
            top_ret = np.where(prev_top > 0, top / prev_top - 1.0, 0.0)
            bot_ret = np.where(prev_bot > 0, bot / prev_bot - 1.0, 0.0)
        ls_ret = (top_ret - bot_ret) / 2.0  # 各分配 50% 资金
        ls_value = np.cumprod(1.0 + ls_ret)

        # 最大回撤: 峰值 = max(1.0, 历史最高), 与原 peak 初值 1.0 一致
        peak = np.maximum(np.maximum.accumulate(ls_value), 1.0)
        max_dd = float(np.min((ls_value - peak) / peak))

        ls_nav = [
            {"date": group_nav[k]["date"], "value": round(float(ls_value[k]), 4)}
            for k in range(len(group_nav))
        ]
        ls_stats = {
            "total_return": round(float(ls_value[-1]) - 1.0, 4),
            "max_drawdown": round(max_dd, 4),
            "top_group": top_col,
            "bottom_group": bottom_col,
        }

        return ls_nav, ls_stats

    @staticmethod
    def _config_to_dict(c: FactorConfig) -> dict:
        return {
            "factor_name": c.factor_name,
            "symbols": c.symbols,
            "start": str(c.start),
            "end": str(c.end),
            "n_groups": c.n_groups,
            "rebalance": c.rebalance,
            "weight": c.weight,
            "fees_pct": c.fees_pct,
            "slippage_bps": c.slippage_bps,
            "asset_type": c.asset_type,
        }

    @staticmethod
    def _batch_config_to_dict(c: FactorBatchConfig, factor_names: list[str]) -> dict:
        return {
            "factor_names": factor_names,
            "symbols": c.symbols,
            "start": str(c.start),
            "end": str(c.end),
            "n_groups": c.n_groups,
            "rebalance": c.rebalance,
            "weight": c.weight,
            "fees_pct": c.fees_pct,
            "slippage_bps": c.slippage_bps,
            "asset_type": c.asset_type,
        }
