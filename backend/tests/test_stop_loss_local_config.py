import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.local_strategy_config import load_stop_loss_monitor_config

# 使用合成配置，避免将用户参数再次写入测试。
POLICY = {"trailingStopPct": 0.12, "stopLossPct": 0.2, "maxHoldDays": 7, "useMa5": False}


def _write(data_dir, value):
    path = data_dir / "local_config" / "stop_loss_monitor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_missing_local_policy_does_not_invent_defaults(tmp_path):
    assert load_stop_loss_monitor_config(tmp_path) is None


def test_policy_reload_uses_private_file_values(tmp_path):
    _write(tmp_path, POLICY)
    assert load_stop_loss_monitor_config(tmp_path) == POLICY
    changed = {**POLICY, "maxHoldDays": 9, "ignored": "not exposed"}
    _write(tmp_path, changed)
    assert load_stop_loss_monitor_config(tmp_path) == {**POLICY, "maxHoldDays": 9}


@pytest.mark.parametrize("invalid", [
    [], {**POLICY, "trailingStopPct": float("nan")}, {**POLICY, "stopLossPct": True},
    {**POLICY, "maxHoldDays": 0}, {**POLICY, "useMa5": "true"}, {},
])
def test_invalid_private_policy_fails_closed(tmp_path, invalid):
    _write(tmp_path, invalid)
    assert load_stop_loss_monitor_config(tmp_path) is None


def test_malformed_private_file_is_not_exposed_in_errors(tmp_path):
    _write(tmp_path, POLICY).write_text("{invalid", encoding="utf-8")
    assert load_stop_loss_monitor_config(tmp_path) is None


def test_settings_api_serves_runtime_policy(tmp_path, monkeypatch):
    from app.api import settings as settings_api
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings_api, "missing_caps", lambda: [])
    monkeypatch.setattr(settings_api, "extras_caps", lambda: [])
    monkeypatch.setattr(settings_api, "tier_label", lambda: "none")
    monkeypatch.setattr(settings_api, "probe_log", lambda: [])
    monkeypatch.setattr(settings_api.tf_client, "current_mode", lambda: "none")
    monkeypatch.setattr(settings_api.tf_client, "current_endpoint", lambda: "")
    _write(tmp_path, POLICY)
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["stop_loss_monitor"] == POLICY
