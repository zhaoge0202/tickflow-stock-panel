"""自定义源实时行情的指数补充链路测试。

契约 (CONTRIBUTING §4 能力路由矩阵): 实时源路由到自定义 provider 时,
quote_service 在 get_realtime() 之外鸭子类型调用可选方法
get_realtime_indices(symbols) 补拉指数 — A 股快照普遍不含指数
(fuyao 实测无指数, 指数在其独立端点)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from app.services import quote_service as qs
from app.services.index_const import CORE_INDEX_SYMBOLS


class _FakeProvider:
    """带指数能力的假自定义源: 记录请求的 symbols, 返回预置 records。"""

    def __init__(self, stocks: list[dict], indices: list[dict]):
        self._stocks = stocks
        self._indices = indices
        self.index_calls: list[list[str]] = []

    def get_realtime(self) -> list[dict]:
        return list(self._stocks)

    def get_realtime_indices(self, symbols: list[str]) -> list[dict]:
        self.index_calls.append(list(symbols))
        wanted = set(symbols)
        return [r for r in self._indices if r["symbol"] in wanted]


class _ProviderNoIndices:
    """未实现可选协议的源: 指数不得报错, 只是不补充。"""

    def get_realtime(self) -> list[dict]:
        return [{"symbol": "600519.SH", "last_price": 1480.0}]


def _stock_rec(symbol: str = "600519.SH") -> dict:
    return {"symbol": symbol, "last_price": 1480.0, "prev_close": 1455.0, "volume": 12345}


def _index_rec(symbol: str) -> dict:
    return {"symbol": symbol, "last_price": 3986.3, "prev_close": 3952.2, "change_pct": 0.0086}


def _service_with_provider(monkeypatch, provider) -> tuple[qs.QuoteService, list[list[dict]]]:
    """构造最小 QuoteService: 自定义源路由 + 捕获 _process_full_market_records 入参。"""
    from app.services import preferences as prefs_mod

    service = qs.QuoteService()
    captured: list[list[dict]] = []
    monkeypatch.setattr(prefs_mod, "get_realtime_data_provider", lambda: "fuyao")
    import app.data_providers.custom as custom_mod

    monkeypatch.setattr(custom_mod, "provider_has_dataset", lambda name, dataset: dataset == "realtime")
    monkeypatch.setattr(custom_mod, "get_provider", lambda name: provider)
    monkeypatch.setattr(
        service, "_process_full_market_records",
        lambda records, *, t0, now_ts: captured.append(records),
    )
    return service, captured


def test_custom_provider_fetch_appends_index_records(monkeypatch):
    provider = _FakeProvider([_stock_rec()], [_index_rec("000001.SH"), _index_rec("399001.SZ")])
    service, captured = _service_with_provider(monkeypatch, provider)
    service._fetch_full_market_quotes()

    assert len(captured) == 1
    symbols = [r["symbol"] for r in captured[0]]
    assert "600519.SH" in symbols and "000001.SH" in symbols and "399001.SZ" in symbols
    # 请求清单 = 核心四只 (无指数监控规则时)
    assert provider.index_calls == [sorted(CORE_INDEX_SYMBOLS)]


def test_custom_provider_monitor_indices_join_fetch(monkeypatch):
    """指数监控规则标的并入请求清单 (quote_service._collect_monitor_index_symbols)。"""
    provider = _FakeProvider([_stock_rec()], [_index_rec("000300.SH")])
    service, _captured = _service_with_provider(monkeypatch, provider)

    class _Engine:
        rules: ClassVar[dict] = {
            "r1": {"enabled": True, "asset_type": "index", "scope": "symbols", "symbols": ["000300.SH"]},
            "r2": {"enabled": False, "asset_type": "index", "scope": "symbols", "symbols": ["000016.SH"]},
            "r3": {"enabled": True, "asset_type": "stock", "scope": "symbols", "symbols": ["600519.SH"]},
        }

    service._app_state = SimpleNamespace(monitor_engine=_Engine())
    service._fetch_full_market_quotes()

    assert provider.index_calls == [sorted(set(CORE_INDEX_SYMBOLS) | {"000300.SH"})]


def test_custom_provider_without_indices_protocol_is_silent(monkeypatch):
    """未实现 get_realtime_indices 的源: 个股 records 照常, 指数不补充不报错。"""
    service, captured = _service_with_provider(monkeypatch, _ProviderNoIndices())
    service._fetch_full_market_quotes()
    assert captured == [[{"symbol": "600519.SH", "last_price": 1480.0}]]


def test_custom_provider_index_fetch_error_is_soft(monkeypatch):
    """指数补充失败软降级: 警告不抛出, 个股 records 仍然进入处理链。"""
    class _Boom:
        def get_realtime(self) -> list[dict]:
            return [_stock_rec()]

        def get_realtime_indices(self, symbols: list[str]) -> list[dict]:
            raise RuntimeError("index endpoint down")

    service, captured = _service_with_provider(monkeypatch, _Boom())
    service._fetch_full_market_quotes()
    assert len(captured) == 1 and captured[0][0]["symbol"] == "600519.SH"
