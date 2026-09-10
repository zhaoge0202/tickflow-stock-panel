"""扩展数据拉取的 API Key 鉴权注入与密钥管理端点。

覆盖: PullConfig.auth 序列化兼容 (历史配置无 auth 字段)、_apply_auth 三型
注入与缺 Key fail-closed、_request_json 共用请求链 (标识头+鉴权)、
api-key GET/PUT 端点的脱敏语义、删除配置时清理残留密钥。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.ext_data import (
    ApiKeyReq,
    PullConfigReq,
    configure_pull,
    get_pull_api_key,
    set_pull_api_key,
)
from app.services import ext_pull
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    ext_api_key_field,
    get_ext_api_key,
)


def _auth_config(auth: dict | None, **pull_kwargs) -> ExtConfig:
    return ExtConfig(
        id="demo",
        label="demo",
        mode="snapshot",
        fields=[ExtField("symbol"), ExtField("score", "float")],
        pull=PullConfig(url="https://api.example.test/data", auth=auth, **pull_kwargs),
    )


# ---------------------------------------------------------------------------
# PullConfig.auth 序列化
# ---------------------------------------------------------------------------

def test_pull_config_auth_roundtrip() -> None:
    auth = {"type": "bearer", "header": "X-Api-Key", "param": "token"}
    pull = PullConfig.from_dict(PullConfig(url="https://x.test", auth=auth).to_dict())
    assert pull.auth == auth


def test_pull_config_without_auth_field_reads_as_none() -> None:
    """历史 config.json 没有 auth 字段 → None (不加鉴权, 行为不变)。"""
    legacy = {"url": "https://x.test", "method": "GET", "headers": {}, "enabled": True}
    assert PullConfig.from_dict(legacy).auth is None


# ---------------------------------------------------------------------------
# _apply_auth 注入
# ---------------------------------------------------------------------------

def _seed_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr("app.secrets_store.load", lambda: {ext_api_key_field("demo"): key})
    monkeypatch.delenv("EXT_DEMO_API_KEY", raising=False)


def test_apply_auth_bearer_injects_header(monkeypatch) -> None:
    _seed_key(monkeypatch, "sk-12345678")
    headers: dict[str, str] = {}
    url = ext_pull._apply_auth("demo", {"type": "bearer"}, "https://x.test/d", headers)
    assert url == "https://x.test/d"
    assert headers["Authorization"] == "Bearer sk-12345678"


def test_apply_auth_header_uses_custom_name(monkeypatch) -> None:
    _seed_key(monkeypatch, "sk-12345678")
    headers: dict[str, str] = {}
    ext_pull._apply_auth("demo", {"type": "header", "header": "X-Api-Key"}, "https://x.test/d", headers)
    assert headers["X-Api-Key"] == "sk-12345678"


def test_apply_auth_query_appends_param_url_encoded(monkeypatch) -> None:
    _seed_key(monkeypatch, "sk+a&b=1")
    headers: dict[str, str] = {}
    url = ext_pull._apply_auth("demo", {"type": "query", "param": "apikey"}, "https://x.test/d?page=1", headers)
    assert url == "https://x.test/d?page=1&apikey=sk%2Ba%26b%3D1"
    assert headers == {}


def test_apply_auth_without_key_fails_closed(monkeypatch) -> None:
    _seed_key(monkeypatch, "")
    with pytest.raises(ValueError, match="未设置 API Key"):
        ext_pull._apply_auth("demo", {"type": "bearer"}, "https://x.test/d", {})


def test_apply_auth_unknown_type_rejected(monkeypatch) -> None:
    _seed_key(monkeypatch, "sk-12345678")
    with pytest.raises(ValueError, match="未知鉴权类型"):
        ext_pull._apply_auth("demo", {"type": "digest"}, "https://x.test/d", {})


# ---------------------------------------------------------------------------
# _request_json 共用请求链 (标识头 + 鉴权 + UA 不被覆盖)
# ---------------------------------------------------------------------------

class _FakeResp:
    def raise_for_status(self) -> None:
        pass

    def json(self):
        return [{"symbol": "600000.SH", "score": 1.0}]


class _FakeClient:
    last: tuple | None = None

    def __init__(self, timeout=None) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        _FakeClient.last = (method, url, kwargs)
        return _FakeResp()


def test_request_json_injects_auth_and_keeps_user_headers(monkeypatch) -> None:
    _seed_key(monkeypatch, "sk-12345678")
    monkeypatch.setattr(ext_pull.httpx, "AsyncClient", _FakeClient)

    pull = PullConfig(
        url="https://x.test/d",
        headers={"User-Agent": "my-agent/1.0"},
        auth={"type": "bearer"},
        date_param="date",
    )
    data = asyncio.run(ext_pull._request_json(pull, "demo", day=__import__("datetime").date(2026, 9, 1)))

    assert data == [{"symbol": "600000.SH", "score": 1.0}]
    method, url, kwargs = _FakeClient.last
    assert method == "GET"
    assert url == "https://x.test/d?date=2026-09-01"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-12345678"
    # 用户显式设置的 User-Agent 优先, 不被标识头覆盖
    assert kwargs["headers"]["User-Agent"] == "my-agent/1.0"


# ---------------------------------------------------------------------------
# api-key 端点 + 删除清理
# ---------------------------------------------------------------------------

def _request(tmp_path) -> SimpleNamespace:
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _make_config(tmp_path) -> ExtConfigStore:
    store = ExtConfigStore(tmp_path)
    store.upsert(ExtConfig(
        id="demo", label="demo", mode="snapshot",
        fields=[ExtField("symbol"), ExtField("score", "float")],
    ))
    return store


def test_api_key_set_get_and_clear(monkeypatch, tmp_path) -> None:
    _make_config(tmp_path)
    req = _request(tmp_path)
    written: dict = {}
    monkeypatch.setattr("app.secrets_store.save", lambda updates: written.update(updates) or updates)
    monkeypatch.setattr("app.secrets_store.load", lambda: written)
    monkeypatch.setattr(
        "app.secrets_store.clear",
        lambda *keys: [written.pop(k, None) for k in keys] or {},
    )

    result = set_pull_api_key(req, "demo", ApiKeyReq(key="sk-12345678"))
    assert result["key_set"] is True
    # 脱敏: 前缀4 + 掩码 + 后缀4, 不含完整明文
    assert "sk-12345678" not in result["masked_key"]
    assert result["masked_key"].startswith("sk-1")
    assert written == {"ext_demo_api_key": "sk-12345678"}

    status = get_pull_api_key(req, "demo")
    assert status["key_set"] is True
    assert "sk-12345678" not in status["masked_key"]

    cleared = set_pull_api_key(req, "demo", ApiKeyReq(key="  "))
    assert cleared["key_set"] is False
    assert cleared["masked_key"] == ""
    assert written == {}


def test_api_key_endpoint_unknown_config_404(tmp_path) -> None:
    req = _request(tmp_path)
    with pytest.raises(Exception, match="不存在"):
        set_pull_api_key(req, "nope", ApiKeyReq(key="sk-x"))


def test_configure_pull_omitted_auth_preserves_existing(monkeypatch, tmp_path) -> None:
    """PUT /pull 请求不带 auth → 沿用现有鉴权; 显式 none → 关闭。"""
    monkeypatch.setattr("app.api.ext_data.pull_scheduler.refresh", lambda *a, **k: None)
    store = _make_config(tmp_path)
    req = _request(tmp_path)

    body = PullConfigReq(url="https://x.test/d", auth=None)
    configure_pull(req, "demo", body)
    assert store.get("demo").pull.auth is None

    # 显式设置 bearer 后, 后续不带 auth 的保存不应清掉它
    configure_pull(req, "demo", PullConfigReq(url="https://x.test/d", auth={"type": "bearer", "header": "X-Key"}))
    configure_pull(req, "demo", PullConfigReq(url="https://x.test/d2"))
    # model_dump 带全默认值 (param 对 bearer 无效但保留, from_dict 可原样读回)
    assert store.get("demo").pull.auth == {"type": "bearer", "header": "X-Key", "param": "token"}
    assert store.get("demo").pull.url == "https://x.test/d2"

    configure_pull(req, "demo", PullConfigReq(url="https://x.test/d", auth={"type": "none"}))
    assert store.get("demo").pull.auth == {"type": "none", "header": "Authorization", "param": "token"}


def test_delete_config_clears_residual_key(monkeypatch, tmp_path) -> None:
    from app.api.ext_data import delete_config

    _make_config(tmp_path)
    req = _request(tmp_path)
    monkeypatch.setattr("app.api.ext_data._refresh_views", lambda request: None)
    cleared: list[str] = []
    monkeypatch.setattr("app.secrets_store.clear", lambda *keys: cleared.extend(keys) or {})

    assert delete_config(req, "demo") == {"status": "deleted"}
    assert cleared == ["ext_demo_api_key"]


def test_get_ext_api_key_env_fallback(monkeypatch) -> None:
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    monkeypatch.setenv("EXT_DEMO_API_KEY", "env-key-123456")
    assert get_ext_api_key("demo") == "env-key-123456"
