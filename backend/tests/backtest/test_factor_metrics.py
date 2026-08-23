from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.factor import (
    FactorBacktestService,
    FactorBatchConfig,
    FactorBatchItem,
    FactorConfig,
    FactorResult,
)
from app.backtest.regime_alignment import (
    align_regime_t_minus_one,
    build_regime_filter_mask,
)


class _Engine:
    def __init__(self, panel: pl.DataFrame, data_dir=None) -> None:
        self.panel = panel
        self.calls = 0
        self.repo = (
            SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
            if data_dir is not None
            else None
        )

    def load_panel(self, symbols, start, end, columns, asset_type):
        self.calls += 1
        selected = [column for column in columns if column in self.panel.columns]
        return self.panel.select(selected)


def _config(
    factor_name: str = "turnover_rate",
    **overrides,
) -> FactorConfig:
    values = {
        "factor_name": factor_name,
        "symbols": None,
        "start": date(2026, 1, 5),
        "end": date(2026, 1, 12),
        "n_groups": 2,
        "rebalance": "daily",
        "fees_pct": 0.0,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return FactorConfig(**values)


def _batch_config(**overrides) -> FactorBatchConfig:
    values = {
        "factor_names": ["turnover_rate"],
        "symbols": None,
        "start": date(2026, 1, 5),
        "end": date(2026, 1, 12),
        "n_groups": 2,
        "rebalance": "daily",
        "fees_pct": 0.0,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return FactorBatchConfig(**values)


def _daily_panel(days: int = 8, symbols: int = 4) -> pl.DataFrame:
    rows = []
    start = date(2026, 1, 5)
    for day in range(days):
        for index in range(symbols):
            rows.append({
                "symbol": f"S{index}",
                "date": start + timedelta(days=day),
                "open": 10.0 + index,
                "high": 10.5 + index,
                "low": 9.5 + index,
                "close": (10.0 + index) * (1.0 + (index - 1) * day * 0.01),
                "volume": 1_000.0,
                "amount": 10_000.0,
                "turnover_rate": float(index + 1),
            })
    return pl.DataFrame(rows)


def test_single_and_batch_use_same_full_price_axis_with_internal_factor_null():
    panel = _daily_panel(days=5, symbols=3).with_columns(
        pl.when((pl.col("symbol") == "S2") & (pl.col("date") == date(2026, 1, 6)))
        .then(None)
        .otherwise(pl.col("turnover_rate"))
        .alias("turnover_rate")
    )
    engine = _Engine(panel)
    service = FactorBacktestService(engine)

    single = service.run(_config(end=date(2026, 1, 9)))
    batch = service.run_batch(_batch_config(end=date(2026, 1, 9)))

    assert single.error is None
    assert batch.results[0].error is None
    assert single.ic_mean == batch.results[0].ic_mean
    assert single.ir == batch.results[0].ir
    assert single.long_short_stats["total_return"] == batch.results[0].long_short_return


def test_daily_forward_returns_join_exact_global_trading_dates_on_suspension():
    dates = [date(2026, 1, 5) + timedelta(days=offset) for offset in range(6)]
    rows = [
        {"symbol": "B", "date": current, "close": 20.0 + index}
        for index, current in enumerate(dates)
    ]
    rows.extend(
        {"symbol": "A", "date": current, "close": 10.0 + index}
        for index, current in enumerate(dates)
        if index != 3
    )
    panel = pl.DataFrame(rows)

    prepared = FactorBacktestService._attach_shared_next_return(
        panel,
        _batch_config(end=dates[-1]),
    )
    first = prepared.filter(
        (pl.col("symbol") == "A") & (pl.col("date") == dates[0])
    ).row(0, named=True)
    second = prepared.filter(
        (pl.col("symbol") == "A") & (pl.col("date") == dates[1])
    ).row(0, named=True)

    assert first["_forward_return_1d"] == pytest.approx(11.0 / 10.0 - 1.0)
    assert first["_forward_return_3d"] is None
    assert first["_forward_return_5d"] == pytest.approx(15.0 / 10.0 - 1.0)
    assert second["_forward_return_3d"] == pytest.approx(14.0 / 11.0 - 1.0)


def test_service_forward_axis_uses_market_partitions_when_selected_universe_has_gap(
    tmp_path,
):
    dates = [date(2026, 1, 5) + timedelta(days=offset) for offset in range(3)]
    panel = pl.DataFrame({
        "symbol": ["A", "A"],
        "date": [dates[0], dates[2]],
        "open": [10.0, 12.0],
        "high": [10.0, 12.0],
        "low": [10.0, 12.0],
        "close": [10.0, 12.0],
        "volume": [1_000.0, 1_000.0],
        "amount": [10_000.0, 12_000.0],
        "turnover_rate": [1.0, 2.0],
    })
    for current in dates:
        partition = tmp_path / "kline_daily_enriched" / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({"date": [current]}).write_parquet(partition / "part.parquet")
    service = FactorBacktestService(_Engine(panel, tmp_path))
    config = _batch_config(start=dates[0], end=dates[-1])

    loaded = service._load_factor_panel(config, ["turnover_rate"])
    prepared = service._attach_shared_next_return(
        loaded,
        config,
        trading_dates=service._global_trading_dates(config),
    )

    first = prepared.filter(pl.col("date") == dates[0]).row(0, named=True)
    assert first["_forward_return_1d"] is None
    assert first["_forward_return_3d"] is None


def test_tie_aware_groups_do_not_split_constant_factor_by_symbol_order():
    panel = pl.DataFrame({
        "symbol": ["C", "A", "D", "B"],
        "date": [date(2026, 1, 5)] * 4,
        "factor": [7.0] * 4,
    })

    first = FactorBacktestService._add_groups(panel, "factor", 5).sort("symbol")
    second = FactorBacktestService._add_groups(
        panel.reverse(), "factor", 5
    ).sort("symbol")

    assert first["_group"].n_unique() == 1
    assert first["_group"].to_list() == second["_group"].to_list()
    assert first["_factor_strength"].to_list() == second["_factor_strength"].to_list()


def test_weekly_uses_first_actual_trading_day_not_monday():
    panel = pl.DataFrame({
        "symbol": ["A", "A", "A", "A"],
        "date": [
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 1, 13),
            date(2026, 1, 14),
        ],
        "close": [10.0, 11.0, 12.0, 13.0],
    })

    result = FactorBacktestService._calc_period_return(panel, "weekly")

    assert result.filter(pl.col("date") == date(2026, 1, 6))["_next_return"][0] == pytest.approx(0.2)
    assert result.filter(pl.col("date") == date(2026, 1, 7))["_next_return"][0] is None


def test_monthly_uses_first_actual_trading_day():
    panel = pl.DataFrame({
        "symbol": ["A", "A", "A", "A"],
        "date": [
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 2, 3),
            date(2026, 2, 4),
        ],
        "close": [10.0, 11.0, 12.0, 13.0],
    })

    result = FactorBacktestService._calc_period_return(panel, "monthly")

    assert result.filter(pl.col("date") == date(2026, 1, 6))["_next_return"][0] == pytest.approx(0.2)
    assert result.filter(pl.col("date") == date(2026, 1, 7))["_next_return"][0] is None


def test_factor_weight_and_decomposed_costs_change_group_results():
    panel = _daily_panel(days=2, symbols=4).with_columns(
        pl.when(pl.col("date") == date(2026, 1, 6))
        .then(
            pl.when(pl.col("symbol") == "S0").then(9.0)
            .when(pl.col("symbol") == "S1").then(10.0)
            .when(pl.col("symbol") == "S2").then(14.3)
            .otherwise(16.9)
        )
        .otherwise(pl.col("close"))
        .alias("close")
    )
    service = FactorBacktestService(_Engine(panel))

    equal = service.run(_config(end=date(2026, 1, 6), weight="equal"))
    weighted = service.run(_config(end=date(2026, 1, 6), weight="factor_weight"))
    costly = service.run(_config(
        end=date(2026, 1, 6),
        weight="factor_weight",
        fees_pct=0.009,
        commission_pct=0.001,
        stamp_tax_pct=0.002,
        slippage_bps=10.0,
    ))

    equal_q2 = next(item for item in equal.group_stats if item["label"] == "Q2")
    weighted_q2 = next(item for item in weighted.group_stats if item["label"] == "Q2")
    costly_q2 = next(item for item in costly.group_stats if item["label"] == "Q2")
    assert weighted_q2["total_return"] != equal_q2["total_return"]
    assert weighted_q2["total_return"] - costly_q2["total_return"] == pytest.approx(0.006)
    assert costly.long_short_stats["total_return"] < weighted.long_short_stats["total_return"]
    assert costly.config["commission_pct"] == 0.001
    assert costly.config["stamp_tax_pct"] == 0.002


def test_factor_v2_metrics_and_defaults_are_backward_compatible():
    default_result = FactorResult(run_id="r", config={})
    default_item = FactorBatchItem(factor_name="f", label="F", group="G")

    assert default_result.methodology_version == "factor_v2"
    assert default_result.yearly_ic == []
    assert default_result.ic_decay == []
    assert default_result.regime_stats == []
    assert default_item.methodology_version == "factor_v2"
    assert default_item.yearly_ic == []

    result = FactorBacktestService(_Engine(_daily_panel())).run(_config())
    assert result.methodology_version == "factor_v2"
    assert result.coverage == 1.0
    assert result.turnover is not None
    assert result.long_short_sharpe is not None
    assert [item["horizon"] for item in result.ic_decay] == [1, 3, 5]
    assert result.yearly_ic[0]["year"] == 2026
    assert result.long_short_stats["portfolio_type"] == "theoretical_factor_spread"
    assert result.long_short_stats["executable_short"] is False


def test_factor_regime_stats_accept_injected_t_minus_one_mapping(tmp_path):
    panel = _daily_panel(days=5)
    market_dates = [date(2026, 1, 2), *panel["date"].unique().sort().to_list()]
    for current in market_dates:
        partition = tmp_path / "kline_daily_enriched" / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({"date": [current]}).write_parquet(partition / "part.parquet")
    regimes = {
        date(2026, 1, 2): {"state": "range", "score": 50},
        date(2026, 1, 5): {"state": "weak", "score": 20},
        date(2026, 1, 6): {"state": "strong", "score": 80},
        date(2026, 1, 7): {"state": "strong", "score": 85},
        date(2026, 1, 8): {"state": "range", "score": 50},
    }

    result = FactorBacktestService(_Engine(panel, tmp_path)).run(
        _config(end=date(2026, 1, 9)),
        regime_by_date=regimes,
    )

    assert result.error is None
    assert {item["state"] for item in result.regime_stats} == {"range", "strong", "weak"}


def test_factor_regime_stats_tolerates_boundary_formal_start(tmp_path):
    """数据边界=正式首日 (如「全部」/「1年」范围起点=本地数据首日) 时不再报错。

    首日无 T-1 环境 → 首日不参与环境分组, 其余日期正常分组, 回测不阻断;
    与策略回测 clamp_formal_start_for_regime 的「首日让渡为预热」同口径。
    """
    panel = _daily_panel(days=4)
    market_dates = panel["date"].unique().sort().to_list()  # 日历从正式首日开始, 无预热日
    for current in market_dates:
        partition = tmp_path / "kline_daily_enriched" / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({"date": [current]}).write_parquet(partition / "part.parquet")
    regimes = {
        date(2026, 1, 5): {"state": "weak", "score": 20},
        date(2026, 1, 6): {"state": "strong", "score": 80},
        date(2026, 1, 7): {"state": "strong", "score": 85},
    }

    result = FactorBacktestService(_Engine(panel, tmp_path)).run(
        _config(end=date(2026, 1, 8)),
        regime_by_date=regimes,
    )

    assert result.error is None
    stats = {item["state"]: item for item in result.regime_stats}
    assert set(stats) == {"strong", "weak"}
    # 首日 (01-05, 无 T-1 环境) 被跳过: weak 桶只含以 01-05 为 T-1 的 01-06
    assert stats["weak"]["n_dates"] == 1
    # strong 桶 = 01-07 (T-1=01-06) + 01-08 (T-1=01-07)
    assert stats["strong"]["n_dates"] == 2


def test_align_first_day_boundary_flag_only_tolerates_first_day():
    labels = ("2026-01-05", "2026-01-06", "2026-01-07")
    regimes = {
        "2026-01-05": ("weak", 20),
        "2026-01-06": ("strong", 80),
    }

    # 默认 (过滤场景): 正式首日=labels[0] 无前驱 → fail-closed
    with pytest.raises(ValueError, match="正式首日"):
        align_regime_t_minus_one(
            labels, regimes, required_start=date(2026, 1, 5), required_end=None,
        )

    # 统计场景: 首日容差 → 首日 None, 其余正常 T-1 对齐
    aligned = align_regime_t_minus_one(
        labels, regimes,
        required_start=date(2026, 1, 5), required_end=None,
        first_day_boundary_ok=True,
    )
    assert aligned == [None, ("weak", 20.0), ("strong", 80.0)]

    # 统计场景内部缺口仍 fail-closed (次日 T-1 缺环境)
    with pytest.raises(ValueError, match="缺少前一交易日环境"):
        align_regime_t_minus_one(
            labels, {"2026-01-05": ("weak", 20)},
            required_start=date(2026, 1, 5), required_end=None,
            first_day_boundary_ok=True,
        )


def test_factor_regime_stats_reject_missing_actual_market_predecessor(tmp_path):
    panel = _daily_panel(days=3)
    market_dates = [date(2026, 1, 2), *panel["date"].unique().sort().to_list()]
    for current in market_dates:
        partition = tmp_path / "kline_daily_enriched" / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({"date": [current]}).write_parquet(partition / "part.parquet")
    regimes = {
        date(2026, 1, 1): {"state": "range", "score": 50},
        date(2026, 1, 5): {"state": "weak", "score": 20},
        date(2026, 1, 6): {"state": "strong", "score": 80},
    }

    with pytest.raises(ValueError, match="2026-01-02"):
        FactorBacktestService(_Engine(panel, tmp_path)).run(
            _config(end=date(2026, 1, 7)),
            regime_by_date=regimes,
        )


def test_align_regime_t_minus_one_is_pure_and_fail_closed_in_required_range():
    labels = ("2026-01-05", "2026-01-06", "2026-01-07")
    regimes = {
        "2026-01-05": ("weak", 20),
        "2026-01-06": {"state": "strong", "score": 80},
    }

    aligned = align_regime_t_minus_one(
        labels,
        regimes,
        required_start=date(2026, 1, 6),
        required_end=date(2026, 1, 7),
    )
    mask = build_regime_filter_mask(
        labels,
        {"states": ["strong"], "min_score": 60},
        regimes,
        required_start=date(2026, 1, 6),
        required_end=date(2026, 1, 7),
    )

    assert aligned == [None, ("weak", 20.0), ("strong", 80.0)]
    assert mask is not None
    assert mask.tolist() == [True, False, True]

    with pytest.raises(ValueError, match="正式首日"):
        align_regime_t_minus_one(
            labels,
            regimes,
            required_start=date(2026, 1, 5),
            required_end=date(2026, 1, 7),
        )

    lean_regimes = {
        "2026-01-05": ("lean_strong", 60),
        "2026-01-06": ("range", 50),
    }
    strong_only_mask = build_regime_filter_mask(
        labels,
        {"states": ["strong"]},
        lean_regimes,
        required_start=date(2026, 1, 6),
        required_end=date(2026, 1, 7),
    )
    assert strong_only_mask is not None
    assert strong_only_mask.tolist() == [True, False, False]

    aggregated_mask = build_regime_filter_mask(
        labels,
        {"states": ["strong", "lean_strong"]},
        lean_regimes,
        required_start=date(2026, 1, 6),
        required_end=date(2026, 1, 7),
    )
    assert aggregated_mask is not None
    assert aggregated_mask.tolist() == [True, True, False]

    with pytest.raises(ValueError, match="缺少前一交易日环境"):
        align_regime_t_minus_one(
            labels,
            {"2026-01-05": ("weak", 20)},
            required_start=date(2026, 1, 6),
            required_end=date(2026, 1, 7),
        )


def test_clamp_formal_start_for_regime():
    from app.backtest.regime_alignment import clamp_formal_start_for_regime

    # 复现「全部」范围: 正式起点 = 面板首日 (数据边界), 环境过滤启用 → 顺延到次日
    labels = ("2025-08-18", "2025-08-19", "2025-08-20")
    filt = {"states": ["strong", "lean_strong"]}
    assert clamp_formal_start_for_regime(labels, date(2025, 8, 18), filt) == date(2025, 8, 19)
    # 正式起点早于面板首日 (用户选的日期早于数据) 同样顺延
    assert clamp_formal_start_for_regime(labels, date(2025, 1, 1), filt) == date(2025, 8, 19)

    # 面板首日早于正式起点 (已有预热日) → 不动
    assert clamp_formal_start_for_regime(labels, date(2025, 8, 19), filt) == date(2025, 8, 19)

    # 过滤未启用 / 空 filter (无 states 无 min_score) / 起点 None → 不动
    assert clamp_formal_start_for_regime(labels, date(2025, 8, 18), None) == date(2025, 8, 18)
    assert clamp_formal_start_for_regime(labels, date(2025, 8, 18), {}) == date(2025, 8, 18)
    assert clamp_formal_start_for_regime(labels, None, filt) is None

    # 面板只有一天, 无法顺延 → 原样返回 (由 fail-closed 校验兜底)
    assert clamp_formal_start_for_regime(("2025-08-18",), date(2025, 8, 18), filt) == date(2025, 8, 18)

    # 顺延后 T-1 对齐不再报「正式首日」错误: 首日环境成为次日 T-1
    regimes = {"2025-08-18": ("weak", 20), "2025-08-19": ("strong", 80)}
    clamped = clamp_formal_start_for_regime(labels, date(2025, 8, 18), filt)
    aligned = align_regime_t_minus_one(labels, regimes, required_start=clamped, required_end=None)
    assert aligned[0] is None and aligned[1] == ("weak", 20.0)
