"""策略 override mtime 签名缓存测试 — 读盘去重, 写入/删除后立即可见, 返回深拷贝。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.strategy import config as strat_config


@pytest.fixture(autouse=True)
def _clean_cache():
    strat_config._override_cache.clear()
    strat_config._override_cache_sig.clear()
    yield
    strat_config._override_cache.clear()
    strat_config._override_cache_sig.clear()


def _patched_loads(monkeypatch, counter: dict):
    real_loads = json.loads

    def _counting(text):
        counter["loads"] += 1
        return real_loads(text)

    monkeypatch.setattr(strat_config.json, "loads", _counting)


def test_second_load_hits_cache_without_disk_parse(tmp_path, monkeypatch):
    strat_config.save_override(tmp_path, "s1", {"params": {"p": 1}})
    counter = {"loads": 0}
    _patched_loads(monkeypatch, counter)

    assert strat_config.load_override(tmp_path, "s1")["params"] == {"p": 1}
    assert strat_config.load_override(tmp_path, "s1")["params"] == {"p": 1}
    assert counter["loads"] == 0 or counter["loads"] == 1, "save 后首次 load 允许一次 parse"
    before = counter["loads"]
    strat_config.load_override(tmp_path, "s1")
    assert counter["loads"] == before, "签名未变时不得重复读盘+parse"


def test_external_file_change_visible(tmp_path):
    strat_config.save_override(tmp_path, "s1", {"params": {"p": 1}})
    assert strat_config.load_override(tmp_path, "s1")["params"]["p"] == 1

    p: Path = tmp_path / "user_data" / "strategy_overrides" / "s1.json"
    p.write_text(json.dumps({"params": {"p": 2}}), encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert strat_config.load_override(tmp_path, "s1")["params"]["p"] == 2


def test_save_override_invalidates_cache(tmp_path):
    strat_config.save_override(tmp_path, "s1", {"params": {"p": 1}})
    assert strat_config.load_override(tmp_path, "s1")["params"]["p"] == 1

    strat_config.save_override(tmp_path, "s1", {"params": {"p": 9}})
    assert strat_config.load_override(tmp_path, "s1")["params"]["p"] == 9


def test_delete_override_invalidates_cache(tmp_path):
    strat_config.save_override(tmp_path, "s1", {"params": {"p": 1}})
    assert strat_config.load_override(tmp_path, "s1") != {}

    strat_config.delete_override(tmp_path, "s1")
    assert strat_config.load_override(tmp_path, "s1") == {}
    assert strat_config.load_override(tmp_path, "s1") == {}


def test_load_returns_deep_copy_not_cached_object(tmp_path):
    strat_config.save_override(tmp_path, "s1", {"params": {"p": 1}, "basic_filter": {"a": 1}})
    first = strat_config.load_override(tmp_path, "s1")
    first["params"]["p"] = 999
    first["extra"] = True

    again = strat_config.load_override(tmp_path, "s1")
    assert again["params"]["p"] == 1
    assert "extra" not in again


def test_basic_filter_cleaning_preserved(tmp_path):
    strat_config.save_override(
        tmp_path, "s1", {"basic_filter": {"keep": 1, "drop": None}},
    )
    data = strat_config.load_override(tmp_path, "s1")
    assert data["basic_filter"] == {"keep": 1}

    strat_config.save_override(tmp_path, "s2", {"basic_filter": {"drop": None}})
    assert "basic_filter" not in strat_config.load_override(tmp_path, "s2")


def test_load_missing_override_returns_empty(tmp_path):
    assert strat_config.load_override(tmp_path, "never_saved") == {}
