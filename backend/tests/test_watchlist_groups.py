"""自选分组持久化与 API 契约。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import watchlist as watchlist_api
from app.config import settings
from app.services import watchlist


def _request():
    repo = MagicMock()
    repo.get_name_map.return_value = {}
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_historical_watchlist_is_read_as_ungrouped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "watchlist.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "added_at": ["2026-08-08T10:00:00"],
        "note": [""],
    }).write_parquet(path)

    assert watchlist.list_symbols()[0]["group_id"] is None


def test_group_lifecycle_preserves_watchlist_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    groups, created = watchlist.create_group("  短线  ", "orange")
    assert groups == [{"id": created["id"], "name": "短线", "color": "orange"}]

    watchlist.add("600000.SH", group_id=created["id"])
    watchlist.add("000001.SZ")
    assert watchlist.list_symbols()[1]["group_id"] == created["id"]

    renamed = watchlist.rename_group(created["id"], "观察", "fuchsia")
    assert renamed[0]["name"] == "观察"
    assert renamed[0]["color"] == "fuchsia"

    remaining, rows = watchlist.delete_group(created["id"])
    assert remaining == []
    assert {row["symbol"] for row in rows} == {"600000.SH", "000001.SZ"}
    assert all(row["group_id"] is None for row in rows)


def test_group_validation_and_assignment_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, created = watchlist.create_group("核心")
    watchlist.add("600000.SH")

    with pytest.raises(ValueError, match="已存在"):
        watchlist.create_group("核心")
    with pytest.raises(ValueError, match="颜色"):
        watchlist.create_group("无效颜色", "black")
    with pytest.raises(ValueError, match="颜色"):
        watchlist.rename_group(created["id"], "核心", "black")
    with pytest.raises(ValueError, match="不存在"):
        watchlist.set_group("600000.SH", "missing")
    with pytest.raises(KeyError):
        watchlist.set_group("000001.SZ", created["id"])

    rows = watchlist.set_group("600000.SH", created["id"])
    assert rows[0]["group_id"] == created["id"]
    rows = watchlist.set_group("600000.SH", None)
    assert rows[0]["group_id"] is None


def test_group_api_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    request = _request()
    created = watchlist_api.create_group(
        watchlist_api.GroupNameRequest(name="中线", color="teal")
    )
    group_id = created["group"]["id"]
    assert created["group"]["color"] == "teal"

    added = watchlist_api.add_one(
        watchlist_api.AddRequest(symbol="600000.SH", group_id=group_id),
        request,
    )
    assert added["symbols"][0]["group_id"] == group_id

    moved = watchlist_api.assign_group(
        "600000.SH",
        watchlist_api.GroupAssignRequest(group_id=None),
        request,
    )
    assert moved["symbols"][0]["group_id"] is None

    with pytest.raises(HTTPException) as exc_info:
        watchlist_api.rename_group("missing", watchlist_api.GroupNameRequest(name="无效"))
    assert exc_info.value.status_code == 404


def test_historical_groups_default_to_sky(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "watchlist_groups.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[{"id":"legacy","name":"旧分组"},'
        '{"id":"invalid","name":"未知颜色","color":"black"}]',
        encoding="utf-8",
    )

    assert watchlist.list_groups() == [
        {"id": "legacy", "name": "旧分组", "color": "sky"},
        {"id": "invalid", "name": "未知颜色", "color": "sky"},
    ]


def test_clear_group_moves_members_to_ungrouped(monkeypatch, tmp_path):
    """清空分组:成员变未分组,分组定义保留。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("芯片")
    watchlist.add("600000.SH", group_id=group["id"])
    watchlist.add("000001.SZ", group_id=group["id"])
    watchlist.add("300750.SZ")  # 不在任何分组

    rows = watchlist.clear_group(group["id"])
    # 3 只都还在,group_id 全部为 None
    assert len(rows) == 3
    assert all(r["group_id"] is None for r in rows)
    # 分组定义仍在
    assert any(g["id"] == group["id"] for g in watchlist.list_groups())

    # 清空不存在的分组 → KeyError
    with pytest.raises(KeyError):
        watchlist.clear_group("missing")


def test_clear_group_api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    request = _request()
    created = watchlist_api.create_group(
        watchlist_api.GroupNameRequest(name="中线", color="teal")
    )
    group_id = created["group"]["id"]
    watchlist.add("600000.SH", group_id=group_id)
    watchlist.add("000001.SZ", group_id=group_id)

    result = watchlist_api.clear_group(group_id, request)
    assert all(s["group_id"] is None for s in result["symbols"])

    # 不存在的分组 → 404
    with pytest.raises(HTTPException) as exc:
        watchlist_api.clear_group("missing", request)
    assert exc.value.status_code == 404
