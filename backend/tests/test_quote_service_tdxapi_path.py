from __future__ import annotations

from app.services.quote_service import QuoteService


class _Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Repo:
    def __init__(self, data_dir):
        self.store = _Store(data_dir)


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
