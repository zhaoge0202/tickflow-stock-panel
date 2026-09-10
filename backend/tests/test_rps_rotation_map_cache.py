"""_load_concept_map_df 缓存契约回归 (#186)。

旧 bug: 正常路径返回 (map_df, count) 元组, 但缓存只存了裸 map_df,
600s 内第二次调用命中缓存返回 DataFrame, 调用方按元组解包会把两列
拆成两个 Series, 概念/行业分析二次访问必报错 (issue 截图定位)。
"""
from __future__ import annotations

import types

import pytest

from app.services import rps_rotation


@pytest.fixture(autouse=True)
def _clear_map_cache():
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    yield
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()


def _fake_repo(tmp_path):
    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _patch_ext(monkeypatch, rows: list[dict]) -> None:
    """替身 ext 配置读取: 免落盘, 聚焦缓存契约本身。"""
    config = types.SimpleNamespace(id="ext_gn_ths")
    monkeypatch.setattr(rps_rotation.ExtConfigStore, "load_all", lambda self: [config])
    monkeypatch.setattr(
        rps_rotation, "_dimension_field",
        lambda cfg, kind: "所属概念" if kind == "concept" else None,
    )
    monkeypatch.setattr(rps_rotation, "_read_ext_rows", lambda data_dir, cfg, field: rows)
    monkeypatch.setattr(
        rps_rotation, "_symbol_keys", lambda row, cfg: [row["symbol"].upper()]
    )


def test_map_cache_hit_returns_same_tuple(tmp_path, monkeypatch):
    _patch_ext(monkeypatch, [
        {"symbol": "s1.SH", "所属概念": "人工智能"},
        {"symbol": "s2.SH", "所属概念": "芯片"},
    ])
    first = rps_rotation._load_concept_map_df(_fake_repo(tmp_path), "concept")
    assert isinstance(first, tuple) and len(first) == 2
    map_df, count = first
    assert count == 2
    assert sorted(map_df["_sym_up"].to_list()) == ["S1.SH", "S2.SH"]

    # 旧 bug: 命中缓存返回裸 DataFrame (只缓存了 map_df), 元组解包变两个 Series
    second = rps_rotation._load_concept_map_df(_fake_repo(tmp_path), "concept")
    assert isinstance(second, tuple) and len(second) == 2
    assert second[0].equals(map_df)
    assert second[1] == count


def test_map_cache_isolated_by_kind(tmp_path, monkeypatch):
    _patch_ext(monkeypatch, [
        {"symbol": "s1.SH", "所属概念": "人工智能"},
    ])
    repo = _fake_repo(tmp_path)
    concept = rps_rotation._load_concept_map_df(repo, "concept")
    industry = rps_rotation._load_concept_map_df(repo, "industry")
    assert concept[1] == 1
    assert industry[1] == 0
    assert industry[0].is_empty()
