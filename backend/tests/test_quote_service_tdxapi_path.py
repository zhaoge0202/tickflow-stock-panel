from __future__ import annotations

from app.services.quote_service import QuoteService


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
