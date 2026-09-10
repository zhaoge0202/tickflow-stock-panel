"""自定义源实时行情的指数补充链路测试。

契约 (CONTRIBUTING §4 能力路由矩阵): 实时源路由到自定义 provider 时,
quote_service 在 get_realtime() 之外鸭子类型调用可选方法
get_realtime_indices(symbols) 补拉指数 — A 股快照普遍不含指数
(fuyao 实测无指数, 指数在其独立端点)。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import ClassVar

import polars as pl

from app.indicators.pipeline import BENCHMARK_INDEX_SYMBOLS
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


def _service_with_provider(
    monkeypatch, provider,
) -> tuple[qs.QuoteService, list[list[dict]], list[bool]]:
    """构造最小 QuoteService: 自定义源路由 + 捕获 _process_full_market_records 入参。"""
    from app.services import preferences as prefs_mod

    service = qs.QuoteService()
    captured: list[list[dict]] = []
    index_cache_replacements: list[bool] = []
    monkeypatch.setattr(prefs_mod, "get_realtime_data_provider", lambda: "fuyao")
    import app.data_providers.custom as custom_mod

    monkeypatch.setattr(custom_mod, "provider_has_dataset", lambda name, dataset: dataset == "realtime")
    monkeypatch.setattr(custom_mod, "get_provider", lambda name: provider)
    monkeypatch.setattr(
        service, "_process_full_market_records",
        lambda records, *, t0, now_ts, replace_index_cache=True, final_boundary_ms=None: (
            captured.append(records),
            index_cache_replacements.append(replace_index_cache),
        ),
    )
    return service, captured, index_cache_replacements


def test_custom_provider_fetch_appends_index_records(monkeypatch):
    provider = _FakeProvider([_stock_rec()], [_index_rec("000001.SH"), _index_rec("399001.SZ")])
    service, captured, replacements = _service_with_provider(monkeypatch, provider)
    service._fetch_full_market_quotes()

    assert len(captured) == 1
    symbols = [r["symbol"] for r in captured[0]]
    assert "600519.SH" in symbols and "000001.SH" in symbols and "399001.SZ" in symbols
    assert replacements == [True]
    # 请求清单 = 核心四只 (无指数监控规则时)
    assert provider.index_calls == [sorted(set(CORE_INDEX_SYMBOLS) | BENCHMARK_INDEX_SYMBOLS)]


def test_custom_provider_monitor_indices_join_fetch(monkeypatch):
    """指数监控规则标的并入请求清单 (quote_service._collect_monitor_index_symbols)。"""
    provider = _FakeProvider([_stock_rec()], [_index_rec("000300.SH")])
    service, _captured, _replacements = _service_with_provider(monkeypatch, provider)

    class _Engine:
        rules: ClassVar[dict] = {
            "r1": {"enabled": True, "asset_type": "index", "scope": "symbols", "symbols": ["000300.SH"]},
            "r2": {"enabled": False, "asset_type": "index", "scope": "symbols", "symbols": ["000016.SH"]},
            "r3": {"enabled": True, "asset_type": "stock", "scope": "symbols", "symbols": ["600519.SH"]},
        }

    service._app_state = SimpleNamespace(monitor_engine=_Engine())
    service._fetch_full_market_quotes()

    assert provider.index_calls == [sorted(set(CORE_INDEX_SYMBOLS) | BENCHMARK_INDEX_SYMBOLS | {"000300.SH"})]


def test_custom_provider_without_indices_protocol_is_silent(monkeypatch):
    """未实现 get_realtime_indices 的源: 个股 records 照常, 指数不补充不报错。"""
    service, captured, replacements = _service_with_provider(monkeypatch, _ProviderNoIndices())
    service._fetch_full_market_quotes()
    assert captured == [[{"symbol": "600519.SH", "last_price": 1480.0}]]
    assert replacements == [True]


def test_custom_provider_index_fetch_error_is_soft(monkeypatch):
    """指数补充失败软降级: 警告不抛出, 个股 records 仍然进入处理链。"""
    class _Boom:
        def get_realtime(self) -> list[dict]:
            return [_stock_rec()]

        def get_realtime_indices(self, symbols: list[str]) -> list[dict]:
            raise RuntimeError("index endpoint down")

    service, captured, replacements = _service_with_provider(monkeypatch, _Boom())
    service._fetch_full_market_quotes()
    assert len(captured) == 1 and captured[0][0]["symbol"] == "600519.SH"
    assert replacements == [False]


def test_custom_provider_index_fetch_failure_preserves_cache(monkeypatch):
    """None 表示指数请求失败: 股票继续更新, 但不得替换上一轮指数缓存。"""
    class _Unavailable:
        def get_realtime(self) -> list[dict]:
            return [_stock_rec()]

        def get_realtime_indices(self, symbols: list[str]) -> None:
            return None

    service, captured, replacements = _service_with_provider(monkeypatch, _Unavailable())
    service._fetch_full_market_quotes()

    assert captured == [[_stock_rec()]]
    assert replacements == [False]


def test_custom_provider_successful_empty_index_fetch_replaces_cache(monkeypatch):
    """空 list 是成功响应: 与失败 None 区分, 仍按现有语义替换缓存。"""
    service, captured, replacements = _service_with_provider(
        monkeypatch, _FakeProvider([_stock_rec()], []),
    )
    service._fetch_full_market_quotes()

    assert captured == [[_stock_rec()]]
    assert replacements == [True]


def _disable_record_processing_side_effects(monkeypatch, service: qs.QuoteService) -> None:
    monkeypatch.setattr(qs, "_persist_last_fetch", lambda fetched_at: None)
    monkeypatch.setattr(service, "_update_volume_delta", lambda records, fetched_at: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    monkeypatch.setattr(service, "_evaluate_monitors", lambda daily, extra: None)


def test_failed_index_refresh_keeps_last_known_good_cache(monkeypatch):
    service = qs.QuoteService()
    _disable_record_processing_side_effects(monkeypatch, service)
    cached = service._build_index_quotes([_index_rec("000001.SH")])
    service._index_quotes_cache = cached
    service._index_symbol_count = cached.height

    service._process_full_market_records(
        [_stock_rec()],
        t0=time.perf_counter(),
        now_ts=time.perf_counter(),
        replace_index_cache=False,
    )

    assert service._index_symbol_count == 1
    assert service.get_index_quotes().to_dicts() == cached.to_dicts()


def test_successful_empty_index_refresh_clears_cache(monkeypatch):
    service = qs.QuoteService()
    _disable_record_processing_side_effects(monkeypatch, service)
    service._index_quotes_cache = service._build_index_quotes([_index_rec("000001.SH")])
    service._index_symbol_count = 1

    service._process_full_market_records(
        [_stock_rec()],
        t0=time.perf_counter(),
        now_ts=time.perf_counter(),
    )

    assert service._index_symbol_count == 0
    assert service.get_index_quotes().is_empty()


# ---- 监控分时注入: 全量分钟健康时股票读本地分区 ----


def _injection_env(monkeypatch, *, healthy, local_df, asset_type="stock", symbols=None):
    """构造 _inject_intraday_signals 最小环境, 捕获传入 evaluator 的 minute_df。"""
    symbols = symbols or {"600519.SH"}
    service = qs.QuoteService()
    service._repo = SimpleNamespace(
        get_minute_batch=lambda syms, d: local_df,
    )
    engine = SimpleNamespace(
        intraday_signal_symbols=lambda at: set(symbols) if at == asset_type else set(),
    )
    minute_svc = SimpleNamespace(is_healthy=lambda: healthy)
    service._app_state = SimpleNamespace(minute_refresh=minute_svc)

    import app.services.quote_service as qsm
    api_calls: list[list[str]] = []
    monkeypatch.setattr(
        qsm, "_noop", qsm.__dict__.get("_noop", None), raising=False)  # 占位无操作
    from app.services.kline_sync import intraday_monitor_support
    monkeypatch.setattr(
        "app.services.quote_service.logger", qsm.logger, raising=False)
    # 打桩 API 拉取路径 (健康时不应被调)
    import app.services.kline_sync as ks
    monkeypatch.setattr(
        ks, "fetch_intraday_monitor_batch",
        lambda symbols, capset, *, now=None: (api_calls.append(list(symbols)), local_df)[1])
    monkeypatch.setattr(
        ks, "intraday_monitor_support",
        lambda capset: {"available": True, "max_symbols": 200, "source": "minute_batch"})

    evaluator = SimpleNamespace(
        evaluate=lambda minute_df, **kw: (captured.append(minute_df), [])[1],
        inject=lambda enriched, signals: enriched,
    )
    captured: list = []
    service._intraday_signal_evaluator = evaluator
    service._intraday_signal_bucket = {}
    return service, engine, api_calls, captured


def test_intraday_signals_read_local_when_healthy(monkeypatch):
    """健康时股票读本地分区, 不触发 API 拉取。"""
    local = pl.DataFrame({
        "symbol": ["600519.SH"], "datetime": ["2026-01-15 09:31:00"],
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5],
        "volume": [1000.0], "amount": [100500.0],
    })
    service, engine, api_calls, captured = _injection_env(monkeypatch, healthy=True, local_df=local)
    enriched = pl.DataFrame({"symbol": ["600519.SH"], "close": [100.5]})
    service._inject_intraday_signals(enriched, engine, asset_type="stock")
    assert api_calls == []          # 未走 API
    assert captured and captured[0].height == 1  # evaluator 拿到本地数据


def test_intraday_signals_fall_back_to_api_when_unhealthy(monkeypatch):
    """不健康 (服务关/挂) 时回落原 API 拉取路径。"""
    service, engine, api_calls, captured = _injection_env(monkeypatch, healthy=False, local_df=pl.DataFrame())
    enriched = pl.DataFrame({"symbol": ["600519.SH"], "close": [100.5]})
    service._inject_intraday_signals(enriched, engine, asset_type="stock")
    assert api_calls == [["600519.SH"]]  # 走了 API


def test_intraday_signals_etf_never_reads_local(monkeypatch):
    """ETF 不在全量分钟 universe: 即使健康也走 API 路径。"""
    service, engine, api_calls, captured = _injection_env(
        monkeypatch, healthy=True, local_df=pl.DataFrame(), asset_type="etf", symbols={"510300.SH"})
    enriched = pl.DataFrame({"symbol": ["510300.SH"], "close": [4.0]})
    service._inject_intraday_signals(enriched, engine, asset_type="etf")
    assert api_calls == [["510300.SH"]]
