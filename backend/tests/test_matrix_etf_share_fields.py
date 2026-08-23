"""ETF/指数矩阵链路不得依赖股本字段 (total_shares/float_shares)。

etf 维表 (index_sync) 物理上只有 symbol/name/code/asset_type, enriched
窄表也不落盘股本字段; 而矩阵缓存档的 common_filter 与 DEFAULT_BASIC_FILTER
都带非 None 的市值界, 依赖解析会因此无条件要求股本字段, 最终在
_resolve_matrix_storage_fields 抛
"matrix parquet fields unavailable: ['float_shares', 'total_shares']"
(用户反馈: ETF 因子挖掘死于「准备共享撮合矩阵」阶段)。非股票资产必须在
依赖解析前中和市值界, 股票行为保持不变。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pyarrow.dataset as pads
import pytest

from app.backtest.matrix import (
    _normalize_matrix_cache_fields,
    _resolve_matrix_storage_fields,
)
from app.backtest.strategy import (
    StrategyDependencyResolver,
    build_matrix_cache_profile,
)
from app.strategy.engine import StrategyEngine

_BUILTIN = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"


def _engine() -> StrategyEngine:
    return StrategyEngine(strategy_dirs=[_BUILTIN, _BUILTIN.parent / "custom"])


def _resolve_research_plan(engine: StrategyEngine, asset_type: str = "stock"):
    research = engine.get("factor_rank_research")
    return StrategyDependencyResolver().resolve(
        research,
        params={
            "scoring": {"turnover_rate": 1.0},
            "directions": {"turnover_rate": "high"},
        },
        basic_filter=dict(research.basic_filter),
        entry_signals=research.entry_signals,
        exit_signals=research.exit_signals,
        overrides={},
        asset_type=asset_type,
    )


def test_etf_plan_does_not_require_share_fields():
    engine = _engine()
    assert engine.get("factor_rank_research").basic_filter.get("market_cap_min") is not None

    etf_plan = _resolve_research_plan(engine, asset_type="etf")
    assert "total_shares" not in etf_plan.instrument_columns
    assert "float_shares" not in etf_plan.instrument_columns

    stock_plan = _resolve_research_plan(engine, asset_type="stock")
    assert "total_shares" in stock_plan.instrument_columns


def test_etf_cache_profile_excludes_share_fields():
    engine = _engine()
    etf_plan = _resolve_research_plan(engine, asset_type="etf")
    etf_profile = build_matrix_cache_profile(
        engine, "etf", requested_plan=etf_plan, requested_forward_bars=5,
    )
    assert "total_shares" not in etf_profile.field_columns
    assert "float_shares" not in etf_profile.field_columns

    stock_plan = _resolve_research_plan(engine, asset_type="stock")
    stock_profile = build_matrix_cache_profile(
        engine, "stock", requested_plan=stock_plan, requested_forward_bars=5,
    )
    assert {"total_shares", "float_shares"} <= set(stock_profile.field_columns)


def _etf_instruments() -> pl.DataFrame:
    # index_sync._fetch_instruments_by_type("etf","etf") 的实际落盘 schema
    return pl.DataFrame({
        "symbol": ["510300.SH", "510500.SH"],
        "name": ["沪深300ETF", "中证500ETF"],
        "code": ["510300", "510500"],
        "asset_type": ["etf", "etf"],
    })


def _etf_dataset(tmp_path: Path) -> pads.Dataset:
    pl.DataFrame({
        "symbol": ["510300.SH", "510500.SH"],
        "date": [date(2024, 1, 2)] * 2,
        "open": [4.0, 6.0],
        "high": [4.1, 6.1],
        "low": [3.9, 5.9],
        "close": [4.0, 6.0],
        "volume": [100.0, 200.0],
        "amount": [400.0, 1200.0],
        "raw_close": [4.0, 6.0],
        "raw_high": [4.1, 6.1],
        "raw_low": [3.9, 5.9],
        "turnover_rate": [0.01, 0.02],
    }).write_parquet(tmp_path / "part.parquet")
    return pads.dataset(str(tmp_path / "part.parquet"), format="parquet")


def test_matrix_storage_fields_resolve_with_etf_instruments(tmp_path):
    engine = _engine()
    etf_plan = _resolve_research_plan(engine, asset_type="etf")
    profile = build_matrix_cache_profile(
        engine, "etf", requested_plan=etf_plan, requested_forward_bars=5,
    )
    requested = (
        set(etf_plan.base_columns)
        | set(etf_plan.instrument_columns)
        | set(etf_plan.matrix_columns)
    )
    build_fields = frozenset(
        _normalize_matrix_cache_fields(frozenset(requested))
        | _normalize_matrix_cache_fields(profile.field_columns)
    )
    assert "total_shares" not in build_fields

    _parquet_fields, matrix_fields, vector_fields = _resolve_matrix_storage_fields(
        _etf_dataset(tmp_path), build_fields, _etf_instruments(),
    )
    assert "total_shares" not in matrix_fields + vector_fields
    assert vector_fields == []


def test_share_fields_still_unavailable_without_sanitization(tmp_path):
    # 反向对照: 若市值界未被中和, ETF 维表下解析仍然失败 —— 锁定失败模式,
    # 防止未来把中和逻辑误删。
    with pytest.raises(ValueError, match=r"matrix parquet fields unavailable"):
        _resolve_matrix_storage_fields(
            _etf_dataset(tmp_path),
            frozenset({"name", "total_shares", "float_shares"}),
            _etf_instruments(),
        )
