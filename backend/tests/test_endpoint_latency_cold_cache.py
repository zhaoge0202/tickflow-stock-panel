"""端点测速接口: 端点清单缓存未预热时的默认轮数不能崩。

_endpoints_cache 的初值是 {"ts": 0.0, "data": None} —— 进程启动后直到第一次
GET /api/settings/endpoints 之前, "data" 键存在但值为 None。前端 endpoints 查询
有 5 分钟 staleTime, 后端(看门狗自愈/重启)重启后用户再点测速, 就落在这个窗口里。
"""
from __future__ import annotations

import pytest

from app.api import settings as settings_api


@pytest.fixture(autouse=True)
def _cold_cache_and_fake_ping(monkeypatch):
    """还原进程刚启动时的缓存初值, 并把网络探测换成固定延迟。"""
    monkeypatch.setattr(settings_api, "_endpoints_cache", {"ts": 0.0, "data": None})

    async def _ping(url: str, timeout: float = 10.0) -> float:
        return 12.5

    monkeypatch.setattr(settings_api, "_http_ping", _ping)


async def test_latency_test_falls_back_to_default_rounds_on_cold_cache():
    """缓存未预热且请求不带 rounds 时, 回退到默认 5 轮而不是 500。"""
    result = await settings_api.test_endpoint(
        settings_api.TestEndpointIn(url="https://api.example.com"),
    )

    assert result["ok"] is True
    assert result["rounds"] == 5
    assert result["success"] == 5
    assert result["median_ms"] == 12.5


async def test_latency_test_uses_manifest_rounds_when_cache_is_warm(monkeypatch):
    """缓存已预热时仍取 endpoints.json 的 testRounds。"""
    monkeypatch.setattr(
        settings_api, "_endpoints_cache", {"ts": 0.0, "data": {"testRounds": 2}},
    )

    result = await settings_api.test_endpoint(
        settings_api.TestEndpointIn(url="https://api.example.com"),
    )

    assert result["rounds"] == 2
    assert result["success"] == 2
