"""股权溢价指数: 市值加权 PE + 10Y 国债 → ERP 与牛熊分档。"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services import equity_premium as ep


def test_classify_regime_thresholds():
    assert ep.classify_regime(4.5)["tone"] == "bull"
    assert ep.classify_regime(3.2)["tone"] == "lean_bull"
    assert ep.classify_regime(2.1)["tone"] == "neutral"
    assert ep.classify_regime(1.2)["tone"] == "lean_bear"
    assert ep.classify_regime(0.5)["tone"] == "bear"
    assert "牛" in ep.classify_regime(5.0)["label"]
    assert "熊" in ep.classify_regime(0.2)["label"]


def test_compute_market_pe_cap_weighted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    metrics_dir = tmp_path / "financials" / "metrics"
    metrics_dir.mkdir(parents=True)
    # A: mcap=100*10=1000, profit=100 → pe_a=10
    # B: mcap=50*20=1000, profit=50  → pe_b=20
    # 市值加权 PE = (1000+1000)/(100+50) = 13.333...
    pl.DataFrame(
        {
            "symbol": ["AAA.SH", "BBB.SZ", "LOSS.SH"],
            "net_profit": [100.0, 50.0, -10.0],
            "total_shares": [10.0, 20.0, 5.0],
        }
    ).write_parquet(metrics_dir / "part.parquet")

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir()
    pl.DataFrame(
        {
            "symbol": ["AAA.SH", "BBB.SZ", "LOSS.SH"],
            "total_shares": [10.0, 20.0, 5.0],
        }
    ).write_parquet(inst_dir / "instruments.parquet")

    rows = [
        {"symbol": "AAA.SH", "close": 100.0},
        {"symbol": "BBB.SZ", "close": 50.0},
        {"symbol": "LOSS.SH", "close": 10.0},  # 负利润剔除
    ]
    info = ep.compute_market_pe(rows, tmp_path)
    assert info is not None
    assert info["sample_count"] == 2
    assert abs(info["pe"] - 13.33) < 0.02
    # earnings_yield 按未四舍五入 PE 计算后再 round, 允许与 100/round(pe) 有微小差
    assert abs(info["earnings_yield"] - 7.5) < 0.02


def test_build_equity_premium_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    metrics_dir = tmp_path / "financials" / "metrics"
    metrics_dir.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["AAA.SH"],
            "net_profit": [100.0],
            "total_shares": [10.0],
        }
    ).write_parquet(metrics_dir / "part.parquet")
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir()
    pl.DataFrame({"symbol": ["AAA.SH"], "total_shares": [10.0]}).write_parquet(
        inst_dir / "instruments.parquet"
    )

    # PE = 100*10 / 100 = 10 → EY = 10%
    # bond 2% → ERP = 8% → 极度低估 · 牛
    monkeypatch.setattr(
        ep,
        "fetch_bond_yield_10y",
        lambda force=False: {"yield": 2.0, "as_of": "2026-08-14", "source": "test"},
    )
    out = ep.build_equity_premium([{"symbol": "AAA.SH", "close": 100.0}], tmp_path)
    assert out["value"] == 8.0
    assert out["pe"] == 10.0
    assert out["earnings_yield"] == 10.0
    assert out["bond_yield_10y"] == 2.0
    assert out["tone"] == "bull"
    assert "牛" in out["label"]


def test_build_equity_premium_missing_bond(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    metrics_dir = tmp_path / "financials" / "metrics"
    metrics_dir.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["AAA.SH"],
            "net_profit": [100.0],
            "total_shares": [10.0],
        }
    ).write_parquet(metrics_dir / "part.parquet")
    monkeypatch.setattr(ep, "fetch_bond_yield_10y", lambda force=False: None)
    out = ep.build_equity_premium([{"symbol": "AAA.SH", "close": 100.0}], tmp_path)
    assert out["value"] is None
    assert out["pe"] == 10.0
    assert "国债" in (out["hint"] or "")
