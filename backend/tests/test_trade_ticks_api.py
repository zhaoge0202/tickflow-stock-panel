"""逐笔成交 API 入参模型测试。"""
from __future__ import annotations

import datetime as dt

from app.api import trade_ticks
from app.api.trade_ticks import PersistReq


def test_persist_req_accepts_date_field():
    req = PersistReq(symbol="000001.SZ", date="2026-07-07")

    assert req.symbol == "000001.SZ"
    assert req.date == dt.date(2026, 7, 7)


def test_get_auction_result_api_uses_tdxapi_provider(monkeypatch):
    closed = False

    class FakeProvider:
        def get_auction_results(self, symbols, trade_date):
            assert symbols == ["002491.SZ"]
            assert trade_date == dt.date(2026, 7, 7)
            return [{
                "symbol": "002491.SZ",
                "trade_date": trade_date,
                "auction_time": "09:25",
                "price": 10.46,
                "volume": 5586,
                "amount": 10.46 * 5586 * 100,
                "trade_index": 1,
                "source": "tdxapi_auction_result_history",
            }]

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(trade_ticks, "TDXAPIProvider", FakeProvider)

    payload = trade_ticks.get_auction_result("002491.SZ", dt.date(2026, 7, 7))

    assert payload["kind"] == "opening_auction_result"
    assert payload["process_available"] is False
    assert payload["count"] == 1
    assert payload["rows"][0]["price"] == 10.46
    assert closed is True
