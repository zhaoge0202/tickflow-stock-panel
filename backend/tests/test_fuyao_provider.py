"""FuyaoProvider 契约与单位标准化测试。

不依赖真实网络: 用假 FuyaoClient 返回样例快照页, 验证字段映射、
百分数→小数制转换 (CONTRIBUTING §3.1)、分页合并、软失败、
能力声明 (未声明数据集回退 tickflow) 与设置页试拉。
"""
from __future__ import annotations

import pytest

from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.provider import FuyaoProvider


class _FakeClient:
    """按调用次数返回预置页, 记录调用供分页断言。snapshot_all 同真实客户端语义。"""

    def __init__(self, pages: list[list[dict]], count: int, error: Exception | None = None, server_ts: int = 0):
        self.pages = pages
        self.count = count
        self.error = error
        self.server_ts = server_ts
        self.calls: list[dict] = []
        self.last_server_ts = server_ts

    def snapshot_page(self, limit=500, offset=0):
        self.calls.append({"limit": limit, "offset": offset})
        if self.error:
            raise self.error
        if not self.pages:
            return [], self.count
        # 按调用次数取页(provider 每轮 offset += len(rows), 页序与调用序一致)
        idx = min(len(self.calls) - 1, len(self.pages) - 1)
        return list(self.pages[idx]), self.count

    def snapshot_all(self):
        """与真实客户端同语义的分页循环 (无页间隔, 测试用)。返回 (rows, server_ts)。"""
        out: list[dict] = []
        offset = 0
        for _ in range(50):
            rows, count = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if count and len(out) >= count:
                break
            offset += len(rows)
        if not out:
            raise fc.FuyaoError("全市场快照为空")
        return out, self.server_ts

    def close(self):
        pass


def _row(thscode: str = "600519.SH", **over):
    """实测快照行结构(2026-08): high_price/low_price/prev_price 命名。"""
    row = {
        "thscode": thscode,
        "last_price": 1480.0,
        "price_change": 25.0,
        "price_change_ratio_pct": 1.72,
        "open_price": 1460.0,
        "high_price": 1490.5,
        "low_price": 1455.0,
        "prev_price": 1455.0,
        "volume": 1234500,
        "turnover": 1.83e9,
    }
    row.update(over)
    return row


def _provider_with(monkeypatch, pages, count=None, error=None, **fake_kwargs):
    fake = _FakeClient(pages, count if count is not None else sum(len(p) for p in pages), error, **fake_kwargs)
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    return FuyaoProvider(), fake


# ---- 单位与字段映射 ----

def test_snapshot_units_and_field_mapping(monkeypatch):
    """核心口径: price_change_ratio_pct 百分数 → change_pct 小数制 (1.72 → 0.0172)。"""
    provider, _ = _provider_with(monkeypatch, [[_row()]])
    records = provider.get_realtime()
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "600519.SH"
    assert r["change_pct"] == pytest.approx(0.0172)
    assert r["change_amount"] == pytest.approx(25.0)
    assert r["prev_close"] == 1455.0
    assert r["open"] == 1460.0
    assert r["high"] == 1490.5
    assert r["low"] == 1455.0
    assert r["volume"] == 1234500
    assert r["amount"] == 1.83e9
    assert r["timestamp"] > 0
    # 快照不提供的字段必须为 None, 不启发式伪造
    assert r["name"] is None
    assert r["amplitude"] is None
    assert r["turnover_rate"] is None


def test_missing_pct_derives_decimal_from_change_amount(monkeypatch):
    """涨跌幅缺失时按 change_amount/prev_close 推导, 仍为小数制。"""
    provider, _ = _provider_with(monkeypatch, [[_row(price_change_ratio_pct=None)]])
    r = provider.get_realtime()[0]
    assert r["change_pct"] == pytest.approx(25.0 / 1455.0)
    assert r["change_amount"] == pytest.approx(25.0)


def test_missing_change_amount_derived_from_prices(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(price_change=None)]])
    r = provider.get_realtime()[0]
    assert r["change_amount"] == pytest.approx(1480.0 - 1455.0)


def test_row_without_thscode_dropped(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(), {"last_price": 1.0}, _row("000001.SZ")]])
    records = provider.get_realtime()
    assert [r["symbol"] for r in records] == ["600519.SH", "000001.SZ"]


def test_all_rows_unrecognized_returns_empty_with_no_fake_data(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[{"foo": "bar"}, {"baz": 1}]])
    assert provider.get_realtime() == []


# ---- 客户端信封解析 (实测结构 vs 文档示例) ----

def _patch_http(monkeypatch, payload, status_code=200):
    class _Resp:
        def json(self):
            return payload
    _Resp.status_code = status_code

    class _Http:
        def get(self, path, params=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(fc.httpx, "Client", lambda **kw: _Http())


def test_client_parses_real_world_envelope(monkeypatch):
    """实测信封(2026-08): data={timestamp, total, item}。"""
    _patch_http(monkeypatch, {
        "code": 0, "message": "success",
        "data": {"timestamp": 1787542612000, "total": 2,
                 "item": [_row(), _row("000001.SZ")]},
    })
    c = fc.FuyaoClient(api_key="k")
    rows, total = c.snapshot_page()
    assert total == 2 and len(rows) == 2
    assert c.last_server_ts == 1787542612000


def test_client_parses_documented_envelope(monkeypatch):
    """官方文档示例信封: data={count, data}。"""
    _patch_http(monkeypatch, {
        "code": 0, "message": "OK",
        "data": {"count": 3, "data": [_row()]},
    })
    c = fc.FuyaoClient(api_key="k")
    rows, total = c.snapshot_page()
    assert total == 3 and len(rows) == 1


def test_client_raises_on_error_code(monkeypatch):
    _patch_http(monkeypatch, {"code": 4001, "message": "rate limited", "data": None})
    c = fc.FuyaoClient(api_key="k")
    with pytest.raises(fc.FuyaoError, match="4001"):
        c.snapshot_page()


# ---- 字段名兼容与服务端时间戳 ----

def test_doc_style_field_names_fallback(monkeypatch):
    """文档示例字段名 (highest_price/lowest_price/prev_close_price) 也能映射。"""
    row = {
        "thscode": "600519.SH", "last_price": 1480.0, "price_change": 25.0,
        "price_change_ratio_pct": 1.72, "open_price": 1460.0,
        "highest_price": 1490.5, "lowest_price": 1455.0, "prev_close_price": 1455.0,
        "volume": 1234500, "turnover": 1.83e9,
    }
    provider, _ = _provider_with(monkeypatch, [[row]])
    r = provider.get_realtime()[0]
    assert r["high"] == 1490.5
    assert r["low"] == 1455.0
    assert r["prev_close"] == 1455.0


def test_realtime_uses_server_timestamp(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]], server_ts=1787542612000)
    assert provider.get_realtime()[0]["timestamp"] == 1787542612000


def test_realtime_falls_back_to_local_time_without_server_ts(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]], server_ts=0)
    assert provider.get_realtime()[0]["timestamp"] > 0


# ---- 分页 ----

def test_snapshot_pagination_merges_pages(monkeypatch):
    page1 = [_row(f"{600000 + i}.SH") for i in range(2)]
    page2 = [_row(f"{688000 + i}.SH") for i in range(1)]
    provider, fake = _provider_with(monkeypatch, [page1, page2], count=3)
    records = provider.get_realtime()
    assert len(records) == 3
    assert len(fake.calls) == 2
    assert fake.calls[1]["offset"] == 2


def test_snapshot_stops_when_page_empty(monkeypatch):
    provider, fake = _provider_with(monkeypatch, [[_row()], []], count=0)
    assert len(provider.get_realtime()) == 1
    assert len(fake.calls) == 2


# ---- 软失败 ----

def test_realtime_error_returns_empty_list(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]], error=fc.FuyaoError("扶摇接口错误 code=4001: 频率超限"))
    assert provider.get_realtime() == []


def test_client_requires_api_key():
    with pytest.raises(fc.FuyaoError):
        fc.FuyaoClient(api_key="")


# ---- 能力声明与注册 ----

def test_datasets_declaration_realtime_only():
    """只声明 realtime; 其他数据集 provider_has_dataset 必须为 False (回退 tickflow)。"""
    config = FuyaoProvider().config
    assert "realtime" in config.datasets
    assert "daily" not in config.datasets
    assert "minute" not in config.datasets
    assert "financial" not in config.datasets


# ---- API Key 解析 (secrets.json > .env, 对齐 tickflow 语义) ----

def test_get_api_key_secrets_store_takes_priority(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {fp.SECRETS_FIELD: "sk-from-ui"})
    assert fp.get_api_key() == "sk-from-ui"


def test_get_api_key_falls_back_to_env(monkeypatch):
    from app import secrets_store
    monkeypatch.setenv(fp.API_KEY_ENV, "sk-from-env")
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    assert fp.get_api_key() == "sk-from-env"


def test_availability_accepts_secrets_store_key(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {fp.SECRETS_FIELD: "sk-from-ui"})
    assert fp.availability() == (True, "ok")


def test_availability_requires_env_key(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv(fp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    ok, reason = fp.availability()
    assert ok is False and fp.API_KEY_ENV in reason


# ---- 先探后存 (probe_api_key) ----

def _patch_client_cls(monkeypatch, fake):
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))


def test_probe_api_key_ok(monkeypatch):
    _patch_client_cls(monkeypatch, _FakeClient([[_row()]], 1))
    ok, reason = fp.probe_api_key("sk-candidate")
    assert ok is True and reason == "ok"


def test_probe_api_key_invalid_key(monkeypatch):
    _patch_client_cls(monkeypatch, _FakeClient([[]], 0, error=fc.FuyaoError("扶摇接口错误 code=1001: 无效 api key")))
    ok, reason = fp.probe_api_key("sk-bad")
    assert ok is False and "无效" in reason


def test_loader_probe_plugin_key_dispatch(monkeypatch):
    import app.plugins.fuyao.provider as provider_mod
    from app.data_providers.custom import loader

    monkeypatch.setattr(provider_mod, "probe_api_key", lambda key: (True, "ok") if key == "good" else (False, "bad"))
    assert loader.probe_plugin_key("fuyao", "good") == (True, "ok")
    assert loader.probe_plugin_key("fuyao", "bad") == (False, "bad")


def test_loader_probe_plugin_key_unsupported_plugin():
    from app.data_providers.custom import loader
    # stock-sdk 未声明 api_key_env → 不支持界面配 Key
    ok, reason = loader.probe_plugin_key("stocksdk", "x")
    assert ok is False and "不支持" in reason
    ok, reason = loader.probe_plugin_key("no_such_plugin", "x")
    assert ok is False and "不存在" in reason


# ---- 保存/清除端点 (直接调用 handler, 先探后存语义) ----

def test_save_plugin_key_invalid_key_not_persisted(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    saved: dict = {}
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda n, k: (False, "Key 无效"))
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates) or updates)
    out = settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="fuyao", api_key="bad"))
    assert out["ok"] is False and out["reason"] == "invalid"
    assert saved == {}  # 无效 Key 不落盘


def test_save_plugin_key_valid_persists_and_rescans(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    saved: dict = {}
    reloaded = []
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda n, k: (True, "ok"))
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates) or updates)
    monkeypatch.setattr(settings_api.secrets_store, "mask", lambda key, prefix=4, suffix=4: "abcd••••wxyz")
    monkeypatch.setattr(custom_sources, "load_all", lambda: reloaded.append(1))
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [{"name": "fuyao", "available": True}])
    out = settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="fuyao", api_key="good-key"))
    assert out["ok"] is True
    assert saved == {"fuyao_api_key": "good-key"}  # 字段名与 provider.SECRETS_FIELD 一致
    assert out["plugin_available"] is True
    assert reloaded == [1]  # 保存后重扫, 插件即刻可用


def test_clear_plugin_key(monkeypatch):
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources

    cleared: list = []
    monkeypatch.setattr(custom_sources, "is_builtin", lambda n: n == "fuyao")
    monkeypatch.setattr(settings_api.secrets_store, "clear", lambda *keys: cleared.extend(keys))
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [{"name": "fuyao", "available": False}])
    out = settings_api.clear_plugin_key("fuyao")
    assert out["ok"] is True and out["plugin_available"] is False
    assert cleared == ["fuyao_api_key"]


def test_manifest_declares_realtime_dataset():
    from app.data_providers.custom import loader
    manifest = loader.plugin_manifest("fuyao")
    assert manifest is not None
    assert manifest["entry"] == "app.plugins.fuyao.provider:FuyaoProvider"
    assert "realtime" in (manifest.get("datasets") or [])
    assert manifest.get("runtime") == "none"
    assert manifest.get("api_key_env") == fp.API_KEY_ENV


def test_hidden_plugin_not_registered():
    """hidden: true 的插件不注册、不在数据源页展示 (优化完成前隐藏 fuyao)。"""
    from app.data_providers.custom import loader
    manifest = loader.plugin_manifest("fuyao")
    assert manifest.get("hidden") is True
    loader._register_one_plugin(manifest)
    assert "fuyao" not in loader._PLUGIN_STATUS
    assert "fuyao" not in loader._PROVIDERS


# ---- 设置页试拉 ----

def test_test_dataset_realtime_preview(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row(), _row("000001.SZ")]], count=5400)
    out = provider.test_dataset("realtime")
    assert out["provider"] == "fuyao"
    assert out["rows"] == 5400
    assert out["preview"][0]["symbol"] == "600519.SH"
    assert out["preview"][0]["change_pct"] == pytest.approx(0.0172)


def test_test_dataset_unsupported_dataset_reports_fallback(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[]])
    out = provider.test_dataset("minute")
    assert "error" in out and "回退" in out["error"]


def test_close_is_idempotent(monkeypatch):
    provider, _ = _provider_with(monkeypatch, [[_row()]])
    provider.close()
    provider.close()
