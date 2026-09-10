"""删除自定义因子: 有引用时必须 fail-closed —— 拒绝的同时定义文件不能已经被删掉。

触发路径: 定义在磁盘上但没进注册表 (load_into_registry 对注册失败的定义只告警跳过,
如复合因子的成员已被强制删除)。此时 get_factor() 为 None, 端点的 404 守卫会走到
第二个分支, 而那个分支本身就会把文件删掉。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.factors import router

FACTOR_ID = "uf_orphan_delete_guard"


@pytest.fixture()
def env(tmp_path):
    """磁盘上有一个未注册的自定义因子, 且被一个策略引用。"""
    factor_dir = tmp_path / "user_data" / "custom_factors"
    factor_dir.mkdir(parents=True)
    factor_path = factor_dir / f"{FACTOR_ID}.json"
    factor_path.write_text(
        json.dumps({
            "id": FACTOR_ID,
            "kind": "custom",
            "label": "孤儿因子",
            "formula": "rank(-ts_sum(change_pct, 5))",
            "version": 1,
            "status": "draft",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "s1.json").write_text(
        json.dumps({"id": "s1", "factors": [FACTOR_ID]}, ensure_ascii=False),
        encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    return TestClient(app), factor_path


def test_delete_with_references_keeps_definition(env):
    """409 拒绝删除时定义必须还在盘上, 否则用户既看不到因子也无法恢复。"""
    client, factor_path = env

    response = client.delete(f"/api/factors/custom/{FACTOR_ID}")

    assert response.status_code == 409
    assert response.json()["detail"]["references"] == ["strategies/s1.json"]
    assert factor_path.exists()


def test_delete_with_force_removes_definition(env):
    """force=true 仍按原语义强制删除。"""
    client, factor_path = env

    response = client.delete(f"/api/factors/custom/{FACTOR_ID}?force=true")

    assert response.status_code == 200
    assert response.json()["removed_references"] == ["strategies/s1.json"]
    assert not factor_path.exists()


def test_delete_missing_factor_returns_404(env):
    """不存在的因子仍返回 404。"""
    client, _ = env

    response = client.delete("/api/factors/custom/uf_not_there_at_all")

    assert response.status_code == 404
