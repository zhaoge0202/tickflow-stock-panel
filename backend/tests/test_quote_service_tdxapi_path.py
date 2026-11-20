from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.quote_service import QuoteService


class _Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Repo:
    def __init__(self, data_dir, etf_symbols=()):
        self.store = _Store(data_dir)
        self._etf_symbols = set(etf_symbols)

    def get_etf_symbol_set(self):
        return set(self._etf_symbols)

    # 行情需求解析 (QuoteDemand) 会读指数维表与 ETF 维表, 桩需同步提供。
    def get_index_symbol_set(self):
        return set()

    def get_etf_instruments(self):
        return pl.DataFrame({"symbol": sorted(self._etf_symbols)})


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_realtime(self, symbols=None):
        self.calls += 1
        assert symbols is None
        return [{
            "symbol": "002491.SZ",
            "name": "测试股",
            "last_price": 10.2,
            "prev_close": 10.0,
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "volume": 100,
            "amount": 102_000,
            "timestamp": 1783485000000,
            "source": "tdxapi",
        }]


def _trade_quote(symbol="600839.SH", *, price=5.45):
    return {
        "symbol": symbol,
        "last_price": price,
        "prev_close": 5.76,
        "open": 5.51,
        "high": 5.74,
        "low": 5.28,
        "volume": 1_763_445,
        "amount": 968_454_016,
        "timestamp": int(datetime(2026, 11, 20, 11, 29, tzinfo=CN_TZ).timestamp() * 1000),
        "price_type": "trade",
        "source": "tdxapi",
    }


def _empty_quote(symbol="600839.SH"):
    return {**_trade_quote(symbol), "last_price": 0, "open": 0, "high": 0,
            "low": 0, "volume": 0, "amount": 5.877471754111438e-39}


def _daily_service(monkeypatch, tmp_path, provider, *, supplement=()):
    """使用真实行情处理与日K构建，仅隔离外部写入和指标重计算。"""
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.services import quote_service as module

    class Repo(_Repo):
        daily = None

        def flush_live_daily(self, frame):
            self.daily = frame.clone()

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(module, "cn_today", lambda: date(2026, 11, 20))
    monkeypatch.setattr(module, "is_trading_weekday", lambda: True)
    monkeypatch.setattr(module, "_persist_last_fetch", lambda _: None)
    service = QuoteService()
    service.set_repo(Repo(tmp_path))
    monkeypatch.setattr(service, "_custom_realtime_supplement_symbols", lambda: list(supplement))
    monkeypatch.setattr(service, "_wanted_quotes", lambda: {
        "stock": set(supplement), "etf": set(), "index": set(),
    })
    monkeypatch.setattr(service, "_append_quote_ticks_if_tdxapi", lambda _: None)
    monkeypatch.setattr(service, "_update_volume_delta", lambda *_: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    monkeypatch.setattr(service, "_flush_live_enriched", lambda *_args, **_kwargs: None)
    return service


def test_targeted_supplement_does_not_erase_valid_market_daily(monkeypatch, tmp_path):
    class Provider:
        def __init__(self):
            self.calls = []

        def get_realtime(self, symbols=None):
            self.calls.append(symbols)
            if symbols is None:
                return [_trade_quote()]
            # 实际故障：定向补拉返回全零，不能覆盖全市场已取得的有效成交。
            return [_empty_quote(symbol) if symbol == "600839.SH" else _trade_quote(symbol)
                    for symbol in symbols]

    provider = Provider()
    service = _daily_service(monkeypatch, tmp_path, provider,
                             supplement=("600839.SH", "000001.SZ"))

    service._fetch_full_market_quotes()

    assert service._repo.daily["symbol"].to_list() == ["000001.SZ", "600839.SH"]
    assert service._repo.daily.filter(pl.col("symbol") == "600839.SH")["close"].item() == 5.45
    assert provider.calls == [None, ["600839.SH", "000001.SZ"]]


def test_market_duplicate_placeholders_keep_valid_daily_and_filter_halted(monkeypatch, tmp_path):
    class Provider:
        def get_realtime(self, symbols=None):
            assert symbols is None
            # 大批次响应中，16 条错误占位行复用了四川长虹的代码。
            return [_trade_quote(), *[_empty_quote() for _ in range(16)], _empty_quote("002122.SZ")]

    service = _daily_service(monkeypatch, tmp_path, Provider())
    enriched_inputs = []
    monkeypatch.setattr(service, "_flush_live_enriched",
                        lambda frame, extra, **kwargs: enriched_inputs.append(frame.clone()))

    service._fetch_full_market_quotes()

    assert service._repo.daily["symbol"].to_list() == ["600839.SH"]
    assert service._repo.daily["close"].item() == 5.45
    assert service._repo.daily["volume"].item() == 1_763_445
    assert len(enriched_inputs) == 1
    assert enriched_inputs[0]["symbol"].to_list() == ["600839.SH"]


@pytest.mark.parametrize("invalid_price", [0, -1, float("nan"), float("inf")])
def test_duplicate_invalid_quote_does_not_remove_valid_daily(monkeypatch, tmp_path, invalid_price):
    class Provider:
        def get_realtime(self, symbols=None):
            return [_trade_quote(), {**_empty_quote(), "last_price": invalid_price}]

    service = _daily_service(monkeypatch, tmp_path, Provider())

    service._fetch_full_market_quotes()

    assert service._repo.daily["symbol"].to_list() == ["600839.SH"]
    assert service._repo.daily["close"].item() == 5.45


def test_duplicate_later_valid_quote_updates_daily(monkeypatch, tmp_path):
    class Provider:
        def get_realtime(self, symbols=None):
            return [_trade_quote(), _trade_quote(price=5.46)]

    service = _daily_service(monkeypatch, tmp_path, Provider())

    service._fetch_full_market_quotes()

    assert service._repo.daily["close"].item() == 5.46


def test_auction_reference_survives_empty_duplicate_without_becoming_daily():
    reference = {**_empty_quote(), "price_type": "auction_reference", "last_price": 5.45,
                 "auction_price": 5.45, "auction_matched_volume": 100}

    records = QuoteService._dedupe_quote_records([reference, _empty_quote()])

    assert records == [reference]
    assert QuoteService._build_daily(records).is_empty()


def test_realtime_mode_tdxapi_bypasses_tickflow_none_tier(monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "none")

    assert QuoteService.realtime_mode() == "full_market"
    assert QuoteService.is_realtime_allowed()


def test_fetch_full_market_quotes_uses_tdxapi_without_tickflow(monkeypatch):
    provider = FakeProvider()
    captured = {}

    import app.tickflow.client as tickflow_client
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: name == "tdxapi" and dataset == "realtime")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(QuoteService, "_custom_realtime_index_symbols", lambda self: [])
    monkeypatch.setattr(
        tickflow_client,
        "get_paid_realtime_client",
        lambda: (_ for _ in ()).throw(AssertionError("不应调用 TickFlow")),
    )

    def fake_process(self, records, *, t0, now_ts, **kwargs):
        captured["records"] = records

    monkeypatch.setattr(QuoteService, "_process_full_market_records", fake_process)

    qs = QuoteService()
    qs._fetch_full_market_quotes()

    assert provider.calls == 1
    assert captured["records"][0]["source"] == "tdxapi"
    assert captured["records"][0]["symbol"] == "002491.SZ"


def test_tdxapi_auction_fields_survive_quote_service_normalization(monkeypatch):
    provider = FakeProvider()
    captured = {}

    import app.tickflow.client as tickflow_client
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(QuoteService, "_custom_realtime_index_symbols", lambda self: [])
    monkeypatch.setattr(
        tickflow_client,
        "get_paid_realtime_client",
        lambda: (_ for _ in ()).throw(AssertionError("不应调用 TickFlow")),
    )
    monkeypatch.setattr(
        QuoteService, "_process_full_market_records",
        lambda self, records, **kwargs: captured.update(records=records),
    )

    original = provider.get_realtime
    provider.get_realtime = lambda symbols=None: [
        {
            **original(symbols)[0],
            "price_type": "auction_reference",
            "market_phase": "preopen_auction",
            "auction_price": 10.2,
            "auction_matched_volume": 637.0,
        }
    ]
    QuoteService()._fetch_full_market_quotes()

    assert captured["records"][0]["price_type"] == "auction_reference"
    assert captured["records"][0]["auction_price"] == 10.2


def test_fetch_full_market_quotes_supplements_manual_positions_for_tdxapi(monkeypatch, tmp_path):
    provider_calls = []

    class ProviderWithSymbols:
        def get_realtime(self, symbols=None):
            provider_calls.append(symbols)
            if symbols is None:
                return [{
                    "symbol": "002491.SZ",
                    "name": "测试股",
                    "last_price": 10.2,
                    "prev_close": 10.0,
                    "source": "tdxapi",
                }]
            return [
                {
                    "symbol": symbol,
                    "name": symbol,
                    "last_price": 3.08,
                    "prev_close": 2.99,
                    "source": "tdxapi",
                }
                for symbol in symbols
            ]

    provider = ProviderWithSymbols()
    captured = {}

    import app.tickflow.client as tickflow_client
    from app.data_providers import custom as custom_sources
    from app.services import manual_positions, preferences

    manual_positions.path(tmp_path).write_text(
        '{"positions":[{"symbol":"589020","shares":600,"cost_price":3.35}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: name == "tdxapi" and dataset == "realtime")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(QuoteService, "_custom_realtime_index_symbols", lambda self: [])
    monkeypatch.setattr(
        tickflow_client,
        "get_paid_realtime_client",
        lambda: (_ for _ in ()).throw(AssertionError("不应调用 TickFlow")),
    )

    def fake_process(self, records, *, t0, now_ts, **kwargs):
        captured["records"] = records

    monkeypatch.setattr(QuoteService, "_process_full_market_records", fake_process)

    qs = QuoteService()
    qs.set_repo(_Repo(tmp_path))
    qs._fetch_full_market_quotes()

    assert provider_calls == [None, ["589020.SH"]]
    assert [row["symbol"] for row in captured["records"]] == ["002491.SZ", "589020.SH"]


def test_fetch_full_market_quotes_supplements_enabled_etfs_for_tdxapi(monkeypatch, tmp_path):
    provider_calls = []

    class ProviderWithSymbols:
        def get_realtime(self, symbols=None):
            provider_calls.append(symbols)
            if symbols is None:
                return [{"symbol": "002491.SZ", "source": "tdxapi"}]
            return [{"symbol": symbol, "source": "tdxapi"} for symbol in symbols]

    provider = ProviderWithSymbols()
    captured = {}

    import app.tickflow.client as tickflow_client
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(preferences, "get_realtime_pull_etf", lambda: True)
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda name, dataset: name == "tdxapi" and dataset == "realtime",
    )
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(QuoteService, "_custom_realtime_index_symbols", lambda self: [])
    monkeypatch.setattr(QuoteService, "_custom_realtime_manual_position_symbols", lambda self: [])
    monkeypatch.setattr(
        tickflow_client,
        "get_paid_realtime_client",
        lambda: (_ for _ in ()).throw(AssertionError("不应调用 TickFlow")),
    )

    def fake_process(self, records, *, t0, now_ts, **kwargs):
        captured["records"] = records

    monkeypatch.setattr(QuoteService, "_process_full_market_records", fake_process)

    qs = QuoteService()
    qs.set_repo(_Repo(tmp_path, etf_symbols={"159881.SZ", "510660.SH"}))
    qs._fetch_full_market_quotes()

    assert provider_calls == [None, ["159881.SZ", "510660.SH"]]
    assert [row["symbol"] for row in captured["records"]] == [
        "002491.SZ",
        "159881.SZ",
        "510660.SH",
    ]


def test_custom_realtime_etf_symbols_syncs_missing_metadata(monkeypatch, tmp_path):
    from app.services import index_sync, preferences

    repo = _Repo(tmp_path)
    sync_calls = []

    def fake_sync(target_repo):
        sync_calls.append(target_repo)
        target_repo._etf_symbols.update({"159881.SZ", "510660.SH"})
        return 2

    monkeypatch.setattr(preferences, "get_realtime_pull_etf", lambda: True)
    monkeypatch.setattr(index_sync, "sync_etf_instruments", fake_sync)

    qs = QuoteService()
    qs.set_repo(repo)

    assert qs._custom_realtime_etf_symbols() == ["159881.SZ", "510660.SH"]
    assert sync_calls == [repo]


def test_build_daily_skips_tdx_preopen_auction_reference():
    df = QuoteService._build_daily([
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "source": "tdxapi",
            "price_type": "auction_reference",
            "market_phase": "preopen_auction",
            "last_price": 22.66,
            "prev_close": 22.61,
            "open": 0,
            "high": 0,
            "low": 0,
            "volume": 0,
            "amount": None,
            "auction_price": 22.66,
            "auction_matched_volume": 1111,
        }
    ])

    assert df.is_empty()


def test_prime_quotes_waits_for_enriched_warmup(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)
    repo.enriched_warming = True
    qs = QuoteService()
    qs.set_repo(repo)
    monkeypatch.setattr(
        qs,
        "_fetch_quotes",
        lambda: (_ for _ in ()).throw(AssertionError("预热期间不应拉取全市场行情")),
    )

    qs._prime_quotes_once()


def test_tdxapi_records_are_submitted_to_mysql_snapshot_writer(monkeypatch, tmp_path):
    from app.services import preferences, quote_snapshot_ingest, quote_tick_store

    records = [
        {"symbol": "002491.SZ", "timestamp": 1_783_485_000_000, "last_price": 10.2},
        {"symbol": "300750.SZ", "timestamp": 1_783_485_000_000, "last_price": 320.0},
    ]
    captured = {}
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")

    def capture_append(data_dir, rows, **kwargs):
        captured["append"] = {"data_dir": data_dir, "rows": rows, "kwargs": kwargs}
        return {}

    monkeypatch.setattr(quote_tick_store, "append_many", capture_append)
    monkeypatch.setattr(QuoteService, "_quote_tick_focus_symbols", lambda self: {"002491.SZ"})

    def capture_submit(rows):
        captured["rows"] = rows
        return True

    monkeypatch.setattr(
        quote_snapshot_ingest.quote_snapshot_ingestor,
        "submit",
        capture_submit,
    )

    qs = QuoteService()
    qs.set_repo(_Repo(tmp_path))
    qs._append_quote_ticks_if_tdxapi(records)

    assert captured["rows"] is records
    assert captured["append"]["data_dir"] == tmp_path
    assert captured["append"]["rows"] is records
    assert captured["append"]["kwargs"] == {"source": "tdxapi", "series_symbols": {"002491.SZ"}}


def test_tdxapi_quote_ticks_skip_weekend_snapshot(monkeypatch, tmp_path):
    from app.services import preferences, quote_snapshot_ingest, quote_tick_store

    calls = []
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(
        "app.services.quote_service.is_trading_weekday", lambda d=None: False,
    )
    monkeypatch.setattr(
        quote_snapshot_ingest.quote_snapshot_ingestor,
        "submit",
        lambda rows: calls.append(("mysql", rows)),
    )
    monkeypatch.setattr(
        quote_tick_store,
        "append_many",
        lambda *args, **kwargs: calls.append(("ticks", args, kwargs)),
    )

    qs = QuoteService()
    qs.set_repo(_Repo(tmp_path))
    qs._append_quote_ticks_if_tdxapi([
        {"symbol": "002491.SZ", "last_price": 10.2},
    ])

    assert calls == []


def test_tdxapi_market_frame_persists_all_records_every_fetch(monkeypatch, tmp_path):
    from app.services import preferences, quote_snapshot_ingest, quote_tick_store

    calls = []
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxapi")
    monkeypatch.setattr(
        quote_snapshot_ingest.quote_snapshot_ingestor,
        "submit",
        lambda rows: True,
    )

    def capture_append(data_dir, rows, **kwargs):
        calls.append({"data_dir": data_dir, "rows": rows, "kwargs": kwargs})
        return {}

    monkeypatch.setattr(quote_tick_store, "append_many", capture_append)

    qs = QuoteService()
    qs.set_repo(_Repo(tmp_path))
    monkeypatch.setattr(qs, "_quote_tick_focus_symbols", lambda: {"002491.SZ"})

    first_batch = [
        {"symbol": "002491.SZ", "timestamp": 1_783_485_000_000, "last_price": 10.2},
        {"symbol": "300750.SZ", "timestamp": 1_783_485_000_000, "last_price": 320.0},
    ]
    second_batch = [
        {"symbol": "002491.SZ", "timestamp": 1_783_485_030_000, "last_price": 10.3},
        {"symbol": "300750.SZ", "timestamp": 1_783_485_030_000, "last_price": 321.0},
    ]
    third_batch = [
        {"symbol": "002491.SZ", "timestamp": 1_783_485_060_000, "last_price": 10.4},
        {"symbol": "300750.SZ", "timestamp": 1_783_485_060_000, "last_price": 322.0},
    ]

    qs._append_quote_ticks_if_tdxapi(first_batch)
    qs._append_quote_ticks_if_tdxapi(second_batch)
    qs._append_quote_ticks_if_tdxapi(third_batch)

    assert len(calls) == 3
    assert [len(c["rows"]) for c in calls] == [2, 2, 2]
    assert all(c["kwargs"].get("source") == "tdxapi" for c in calls)
    assert all(c["kwargs"].get("series_symbols") == {"002491.SZ"} for c in calls)
