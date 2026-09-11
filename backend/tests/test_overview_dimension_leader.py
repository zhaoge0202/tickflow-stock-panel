"""市场总览「领涨股」选取: 涨跌幅 0.00% 是有效值, 不能被当成缺失值。"""
from __future__ import annotations

import json

import polars as pl

from app.services.market_overview_builder import _dimension_rank


def _fake_repo(tmp_path):
    import types

    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _write_concept_ext(tmp_path, mapping: dict[str, str]) -> None:
    """写一张 snapshot 模式的概念扩展表 (symbol → 所属概念)。"""
    cfg_dir = tmp_path / "ext_data" / "concept_tbl"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath("config.json").write_text(
        json.dumps({
            "id": "concept_tbl",
            "label": "概念表",
            "mode": "snapshot",
            "fields": [{"name": "所属概念", "dtype": "string", "label": "所属概念"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    pl.DataFrame({
        "symbol": list(mapping.keys()),
        "所属概念": list(mapping.values()),
    }).write_parquet(cfg_dir / "part.parquet")


def test_flat_stock_can_be_leader_of_a_falling_concept(tmp_path):
    """全概念下跌、最强的一只恰好平盘(0.00%)时, 领涨股必须是那只平盘股。"""
    _write_concept_ext(tmp_path, {
        "000001.SZ": "人工智能",
        "000002.SZ": "人工智能",
        "000003.SZ": "人工智能",
    })
    rows = [
        {"symbol": "000001.SZ", "name": "跌一", "change_pct": -0.01, "amount": 1e8},
        {"symbol": "000002.SZ", "name": "平盘", "change_pct": 0.0, "amount": 2e8},
        {"symbol": "000003.SZ", "name": "跌三", "change_pct": -0.03, "amount": 3e8},
    ]

    result = _dimension_rank(rows, _fake_repo(tmp_path), "concept")

    items = {item["name"]: item for item in result["lagging"]}
    assert "人工智能" in items
    leader = items["人工智能"]["leader"]
    assert leader["name"] == "平盘"
    assert leader["change_pct"] == 0.0


def test_leader_falls_back_to_none_pct_last(tmp_path):
    """change_pct 缺失(None)的成分股仍排在所有有值的成分股之后。"""
    _write_concept_ext(tmp_path, {
        "000001.SZ": "芯片",
        "000002.SZ": "芯片",
    })
    rows = [
        {"symbol": "000001.SZ", "name": "无行情", "change_pct": None, "amount": 1e8},
        {"symbol": "000002.SZ", "name": "微跌", "change_pct": -0.02, "amount": 1e8},
    ]

    result = _dimension_rank(rows, _fake_repo(tmp_path), "concept")

    items = {item["name"]: item for item in result["lagging"]}
    assert items["芯片"]["leader"]["name"] == "微跌"


def test_leader_still_picks_max_when_all_positive(tmp_path):
    """普通情形不受影响: 全部上涨时仍取涨幅最大的一只。"""
    _write_concept_ext(tmp_path, {
        "000001.SZ": "光伏",
        "000002.SZ": "光伏",
    })
    rows = [
        {"symbol": "000001.SZ", "name": "涨多", "change_pct": 0.05, "amount": 1e8},
        {"symbol": "000002.SZ", "name": "涨少", "change_pct": 0.01, "amount": 1e8},
    ]

    result = _dimension_rank(rows, _fake_repo(tmp_path), "concept")

    items = {item["name"]: item for item in result["leading"]}
    assert items["光伏"]["leader"]["name"] == "涨多"
