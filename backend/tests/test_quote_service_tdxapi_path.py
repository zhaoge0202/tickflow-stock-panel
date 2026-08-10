from __future__ import annotations

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

    def fake_process(self, records, *, t0, now_ts):
        captured["records"] = records

    monkeypatch.setattr(QuoteService, "_process_full_market_records", fake_process)

    qs = QuoteService()
    qs._fetch_full_market_quotes()

    assert provider.calls == 1
    assert captured["records"][0]["source"] == "tdxapi"
    assert captured["records"][0]["symbol"] == "002491.SZ"


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

    def fake_process(self, records, *, t0, now_ts):
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

    def fake_process(self, records, *, t0, now_ts):
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
