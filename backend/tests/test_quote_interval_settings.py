"""行情轮询间隔接口: quote_service 缺失时的兜底分支不能抛 AttributeError (issue #261)。"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.settings import QuoteIntervalIn, update_quote_interval


def _request_without_quote_service() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=None)))


def test_update_quote_interval_without_service_returns_defaults():
    result = update_quote_interval(
        QuoteIntervalIn(interval=6.0), request=_request_without_quote_service()
    )

    assert result == {"interval": 6.0, "min_interval": 6.0, "max_interval": 60.0}


def test_update_quote_interval_without_service_keeps_requested_interval():
    result = update_quote_interval(
        QuoteIntervalIn(interval=12.0), request=_request_without_quote_service()
    )

    assert result["interval"] == 12.0
    assert result["min_interval"] == 6.0
    assert result["max_interval"] == 60.0
