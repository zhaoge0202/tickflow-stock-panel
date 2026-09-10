"""涨幅轮动矩阵结果缓存必须按 days 区分。

build_rps_rotation 只按 days 换算出的日历窗口读 enriched (days=7 只读 24 个
自然日), 但结果缓存键是 "{kind}|{level}|{latest}" —— 不含 days。先看 7 日
再切到 30 日, 120s TTL 内会命中那份按 7 日窗口算出来的矩阵, 前端拿到的列数
比请求的少。
"""
from __future__ import annotations

import types
from datetime import date, timedelta

import polars as pl
import pytest

from app.services import rps_rotation

_LATEST = date(2026, 6, 30)
_HISTORY_DAYS = 80
_MEMBERS = ("人工智能", "芯片")


@pytest.fixture(autouse=True)
def _clear_caches():
    rps_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    yield
    rps_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()


def _history() -> pl.DataFrame:
    """两只票在 80 个连续自然日上的 change_pct(小数制)。"""
    rows = []
    for offset in range(_HISTORY_DAYS):
        day = _LATEST - timedelta(days=offset)
        rows.append({"symbol": "S1.SH", "date": day, "change_pct": 0.01})
        rows.append({"symbol": "S2.SH", "date": day, "change_pct": -0.01})
    return pl.DataFrame(rows)


def _fake_repo() -> types.SimpleNamespace:
    history = _history()

    def get_enriched_range(start, end, columns=None):
        df = history.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        return df.select(columns) if columns else df

    return types.SimpleNamespace(
        _enriched_history_cache=history,
        get_enriched_range=get_enriched_range,
        store=types.SimpleNamespace(data_dir=None),
    )


@pytest.fixture
def repo(monkeypatch) -> types.SimpleNamespace:
    map_df = pl.DataFrame(
        {"_sym_up": ["S1.SH", "S2.SH"], "concept": list(_MEMBERS)},
        schema={"_sym_up": pl.Utf8, "concept": pl.Utf8},
    )
    monkeypatch.setattr(
        rps_rotation, "_load_concept_map_df", lambda _repo, kind: (map_df, len(_MEMBERS))
    )
    return _fake_repo()


def test_widening_days_after_a_narrow_request_returns_all_days(repo):
    """先请求 7 日再请求 30 日, 第二次必须拿到 30 列。

    旧实现: 缓存键不含 days, 第二次命中第一次那份只有 25 列的矩阵。
    """
    narrow = rps_rotation.build_rps_rotation(repo, days=7)
    assert len(narrow["dates"]) == 7

    wide = rps_rotation.build_rps_rotation(repo, days=30)
    assert len(wide["dates"]) == 30
    assert len(wide["columns"]) == 30


def test_narrowing_days_after_a_wide_request_still_slices(repo):
    """反方向仍要按请求截断 (宽窗缓存可以复用, 但只返回请求的天数)。"""
    wide = rps_rotation.build_rps_rotation(repo, days=30)
    assert len(wide["dates"]) == 30

    narrow = rps_rotation.build_rps_rotation(repo, days=7)
    assert len(narrow["dates"]) == 7
    assert narrow["dates"] == wide["dates"][:7]


def test_same_days_request_hits_cache(repo, monkeypatch):
    """同一 days 的重复请求仍走缓存, 不重新读 enriched。"""
    rps_rotation.build_rps_rotation(repo, days=12)

    def _boom(*args, **kwargs):
        raise AssertionError("缓存未命中: 又读了一次 enriched")

    monkeypatch.setattr(repo, "get_enriched_range", _boom)
    again = rps_rotation.build_rps_rotation(repo, days=12)
    assert len(again["dates"]) == 12
