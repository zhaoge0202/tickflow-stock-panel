"""preferences mtime 缓存测试 — 读盘去重, 且外部修改/自身写入后立即可见。"""
from __future__ import annotations

import json
import os

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def _patched_loads(monkeypatch, counter: dict):
    real_loads = json.loads

    def _counting(text):
        counter["loads"] += 1
        return real_loads(text)

    monkeypatch.setattr(preferences.json, "loads", _counting)


def test_second_load_hits_cache_without_disk_parse(_isolated, monkeypatch):
    _isolated.write_text(json.dumps({"realtime_quotes_enabled": True}), encoding="utf-8")
    counter = {"loads": 0}
    _patched_loads(monkeypatch, counter)

    assert preferences.load()["realtime_quotes_enabled"] is True
    assert preferences.load()["realtime_quotes_enabled"] is True
    assert counter["loads"] == 1, "签名未变时第二次 load 不得重复读盘+parse"


def test_external_file_change_invalidates_cache(_isolated, monkeypatch):
    _isolated.write_text(json.dumps({"realtime_quote_interval": 6.0}), encoding="utf-8")
    assert preferences.load()["realtime_quote_interval"] == 6.0

    _isolated.write_text(json.dumps({"realtime_quote_interval": 3.0}), encoding="utf-8")
    # 同尺寸修改且 mtime 粒度可能不变时, 显式推进 mtime 模拟真实场景
    st = _isolated.stat()
    os.utime(_isolated, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert preferences.load()["realtime_quote_interval"] == 3.0


def test_save_then_load_sees_merged_values(_isolated):
    _isolated.write_text(json.dumps({"a": 1}), encoding="utf-8")
    out = preferences.save({"b": 2})
    assert out == {"a": 1, "b": 2}
    assert preferences.load() == {"a": 1, "b": 2}


def test_interval_setter_invalidates_cache(_isolated):
    preferences.set_realtime_quote_interval(2.0)
    assert preferences.load()["realtime_quote_interval"] == 2.0


def test_load_returns_copy_not_cached_object(_isolated):
    _isolated.write_text(json.dumps({"k": [1, 2]}), encoding="utf-8")
    first = preferences.load()
    first["k"].append(3)
    first["extra"] = True
    again = preferences.load()
    assert again == {"k": [1, 2]}


def test_mining_schedule_defaults_are_disabled(_isolated):
    assert preferences.get_mining_schedule() == {
        "mining_schedule_enabled": False,
        "mining_schedule_weekday": 4,
        "mining_budget_profile": "balanced",
    }


def test_mining_schedule_invalid_stored_values_fail_closed(_isolated):
    _isolated.write_text(
        json.dumps(
            {
                "mining_schedule_enabled": "false",
                "mining_schedule_weekday": True,
                "mining_budget_profile": None,
            }
        ),
        encoding="utf-8",
    )

    assert preferences.get_mining_schedule() == {
        "mining_schedule_enabled": False,
        "mining_schedule_weekday": 4,
        "mining_budget_profile": "balanced",
    }


def test_mining_schedule_setter_saves_group_once(monkeypatch):
    calls = []
    monkeypatch.setattr(preferences, "save", lambda updates: calls.append(updates) or updates)

    result = preferences.set_mining_schedule(True, 2, "strict")

    assert result == {
        "mining_schedule_enabled": True,
        "mining_schedule_weekday": 2,
        "mining_budget_profile": "strict",
    }
    assert calls == [result]


@pytest.mark.parametrize("weekday", [-1, 5, True])
def test_mining_schedule_setter_rejects_invalid_weekday(weekday):
    with pytest.raises(ValueError, match="weekday"):
        preferences.set_mining_schedule(True, weekday, "balanced")


def test_mining_schedule_setter_rejects_invalid_profile():
    with pytest.raises(ValueError, match="profile"):
        preferences.set_mining_schedule(True, 4, "exploratory")
