"""周末非交易日不落"当日快照"日K。

数据源在非交易日仍会返回以上一交易日快照冒充的"当日"行情 (OHLCV 与上一
交易日完全一致, 涨跌幅 0), 若按 cn_today() 写盘会产生日期错误的假日K
(实测: 2026-08-30 周日分区复制了 2026-08-28 周五的 K 线)。
"""
from __future__ import annotations

import threading
import types
from datetime import date

import polars as pl

from app.market_time import is_trading_weekday
from app.services import kline_sync
from app.services.quote_service import QuoteService

_SUNDAY = date(2026, 8, 30)
_FRIDAY = date(2026, 8, 28)


def test_is_trading_weekday_rejects_weekend():
    assert is_trading_weekday(_FRIDAY)  # 周五
    assert is_trading_weekday(date(2026, 8, 31))  # 周一
    assert not is_trading_weekday(date(2026, 8, 29))  # 周六
    assert not is_trading_weekday(_SUNDAY)


# ---- kline_sync.sync_daily_by_quotes (盘后管道/修复路径) ----

class _FakeQuotes:
    @staticmethod
    def get_by_universes(universes):
        return [{
            "symbol": "002491.SZ", "open": 23.08, "high": 23.55, "low": 22.16,
            "last_price": 22.23, "volume": 2477541, "amount": 5647315456.0,
        }]


class _FakeClient:
    quotes = _FakeQuotes


class _FakeRepo:
    def __init__(self):
        self.flushed: list[pl.DataFrame] = []

    def flush_live_daily(self, df):
        self.flushed.append(df)
        return df.height


def test_sync_daily_by_quotes_skips_weekend(monkeypatch):
    monkeypatch.setattr("app.tickflow.client.get_client", lambda: _FakeClient())
    monkeypatch.setattr(kline_sync, "cn_today", lambda: _SUNDAY)
    repo = _FakeRepo()

    assert kline_sync.sync_daily_by_quotes(repo) == 0
    assert repo.flushed == []


def test_sync_daily_by_quotes_flushes_on_trading_weekday(monkeypatch):
    monkeypatch.setattr("app.tickflow.client.get_client", lambda: _FakeClient())
    monkeypatch.setattr(kline_sync, "cn_today", lambda: _FRIDAY)
    repo = _FakeRepo()

    assert kline_sync.sync_daily_by_quotes(repo) == 1
    assert repo.flushed[0]["date"][0] == _FRIDAY


# ---- quote_service._process_full_market_records (实时轮询路径) ----

_RECORD = {
    "symbol": "002491.SZ", "name": "测试股", "last_price": 22.23,
    "prev_close": 22.23, "open": 23.08, "high": 23.55, "low": 22.16,
    "volume": 2477541, "amount": 5647315456.0, "timestamp": 1787960100000,
    "source": "tdxapi",
}


class _GuardRepo:
    def __init__(self):
        self.daily_flushes: list[str] = []
        self.enriched_flushes: list[str] = []

    def get_index_symbol_set(self):
        return set()

    def get_etf_instruments(self):
        return pl.DataFrame()

    def flush_live_daily(self, df):
        self.daily_flushes.append("stock")

    def flush_live_daily_asset(self, asset_type, df):
        self.daily_flushes.append(asset_type)


def _run_process(monkeypatch, flush_daily_today: bool) -> _GuardRepo:
    from app.services import preferences

    repo = _GuardRepo()
    svc = QuoteService.__new__(QuoteService)
    svc._repo = repo
    svc._app_state = None
    svc._lock = threading.Lock()
    svc._fetch_time = 0.0
    svc._fetch_ms = 0.0
    svc._fetched_at = 0.0
    svc._symbol_count = 0
    svc._index_symbol_count = 0
    svc._etf_symbol_count = 0
    svc._index_quotes_cache = {}
    svc._evaluate_monitors = lambda *a, **k: None
    svc._broadcast_quote_updated = lambda: None
    svc._append_quote_ticks_if_tdxapi = lambda records: None
    svc._flush_live_enriched = lambda df, extra, asset_type="stock", merge=False: repo.enriched_flushes.append(asset_type)

    monkeypatch.setattr("app.services.quote_service._persist_last_fetch", lambda fetched_at: None)
    monkeypatch.setattr("app.services.quote_service.is_trading_weekday", lambda d=None: flush_daily_today)
    monkeypatch.setattr(preferences, "get_realtime_index_symbols", lambda: [])

    svc._process_full_market_records([dict(_RECORD)], t0=0.0, now_ts=0.0)
    return repo


def test_full_market_records_skip_daily_flush_on_weekend(monkeypatch):
    repo = _run_process(monkeypatch, flush_daily_today=False)
    assert repo.daily_flushes == []
    assert repo.enriched_flushes == []


def test_full_market_records_flush_daily_on_trading_weekday(monkeypatch):
    repo = _run_process(monkeypatch, flush_daily_today=True)
    assert repo.daily_flushes == ["stock"]
    assert repo.enriched_flushes == ["stock"]


# ---- quote_service._fetch_watchlist_quotes (自选实时路径) ----


class _FakeTFQuotes:
    @staticmethod
    def get(symbols=None):
        return [{
            "symbol": "002491.SZ", "name": "测试股", "last_price": 22.23,
            "prev_close": 22.23, "open": 23.08, "high": 23.55, "low": 22.16,
            "volume": 2477541, "amount": 5647315456.0, "timestamp": 1787960100000,
            "session": "close",
        }]


class _FakeTF:
    quotes = _FakeTFQuotes


class _WatchRepo:
    def __init__(self):
        self.daily_merges: list[str] = []
        self.enriched_merges: list[str] = []

    def get_index_symbol_set(self):
        return set()

    def get_etf_symbol_set(self):
        return set()

    def merge_live_daily_asset(self, asset_type, df):
        self.daily_merges.append(asset_type)


def _run_watchlist_fetch(monkeypatch, flush_daily_today: bool) -> _WatchRepo:
    repo = _WatchRepo()
    svc = QuoteService.__new__(QuoteService)
    svc._repo = repo
    svc._app_state = None
    svc._lock = threading.Lock()
    svc._fetch_time = 0.0
    svc._fetch_ms = 0.0
    svc._fetched_at = 0.0
    svc._symbol_count = 0
    svc._index_symbol_count = 0
    svc._etf_symbol_count = 0
    svc._index_quotes_cache = None
    svc._broadcast_quote_updated = lambda: None
    svc._evaluate_monitors = lambda *a, **k: None
    svc._flush_live_enriched = (
        lambda df, extra, asset_type="stock", merge=False: repo.enriched_merges.append(asset_type)
    )

    monkeypatch.setattr(
        "app.services.preferences.get_realtime_watchlist_symbols",
        lambda: ["002491.SZ"],
    )
    monkeypatch.setattr("app.services.quote_service._persist_last_fetch", lambda fetched_at: None)
    monkeypatch.setattr(
        "app.services.quote_service.is_trading_weekday", lambda d=None: flush_daily_today
    )
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: _FakeTF())
    monkeypatch.setattr("app.tickflow.policy.detect_capabilities", lambda: object())
    monkeypatch.setattr(
        "app.tickflow.rate_limits.resolve_limit",
        lambda capset, cap, **kwargs: types.SimpleNamespace(batch=5, rpm=600),
    )

    svc._fetch_watchlist_quotes()
    return repo


def test_watchlist_quotes_skip_daily_merge_on_weekend(monkeypatch):
    repo = _run_watchlist_fetch(monkeypatch, flush_daily_today=False)
    assert repo.daily_merges == []
    assert repo.enriched_merges == []


def test_watchlist_quotes_merge_on_trading_weekday(monkeypatch):
    repo = _run_watchlist_fetch(monkeypatch, flush_daily_today=True)
    assert repo.daily_merges == ["stock"]
    assert repo.enriched_merges == ["stock"]


# ---- trade_tick_ingest (逐笔成交入库) ----


def test_tick_ingest_rejects_weekend_date():
    from app.services.trade_tick_ingest import TradeTickIngestor

    result = TradeTickIngestor().enqueue("002491.SZ", _SUNDAY)
    assert result["status"] == "rejected"
    assert result["date"] == "2026-08-30"


def test_tick_ingest_queues_on_trading_weekday(monkeypatch):
    from app.services.trade_tick_ingest import TradeTickIngestor

    monkeypatch.setattr(
        "app.services.trade_tick_ingest.trade_tick_mysql_store.enabled", lambda: True
    )
    result = TradeTickIngestor().enqueue("002491.SZ", _FRIDAY)
    assert result["status"] == "queued"
    assert result["date"] == _FRIDAY.isoformat()


# ---- api.trade_ticks.list_trade_ticks (分笔读取路径) ----


def test_trade_ticks_api_returns_empty_on_weekend(monkeypatch):
    from app.api import trade_ticks as tt

    def _fail_provider(*a, **k):
        raise AssertionError("周末不应访问 tdx 实时源")

    monkeypatch.setattr(tt, "TDXAPIProvider", _fail_provider)

    resp = tt.list_trade_ticks(
        symbol="002491.SZ", trade_date=_SUNDAY, source="auto",
        mode="all", limit=100, order="desc",
    )
    assert resp["rows"] == []
    assert resp["source"] == "none"
    assert resp["warning"]
    assert resp["date"] == "2026-08-30"


def test_trade_ticks_api_serves_trading_weekday(monkeypatch):
    from app.api import trade_ticks as tt

    class _FakeProvider:
        def get_trade_ticks(self, symbol, day, mode="recent", limit=None):
            return [{"seq_in_day": 1, "price": 22.23}]

        def close(self):
            return None

    monkeypatch.setattr(tt, "TDXAPIProvider", _FakeProvider)
    monkeypatch.setattr(tt.trade_tick_mysql_store, "configured", lambda: False)

    resp = tt.list_trade_ticks(
        symbol="002491.SZ", trade_date=_FRIDAY, source="auto",
        mode="all", limit=100, order="desc",
    )
    assert resp["source"] == "tdxapi"
    assert resp["count"] == 1
    assert resp["date"] == "2026-08-28"
