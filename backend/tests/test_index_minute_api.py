"""指数分钟 API 契约: 历史日期路由与 TTL 缓存。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from app.api import indices


@pytest.fixture(autouse=True)
def fixed_context(monkeypatch):
    monkeypatch.setattr(indices, "cn_today", lambda: date(2026, 9, 14))
    monkeypatch.setattr(indices.preferences, "get_minute_data_provider", lambda: "custom")
    indices._index_minute_cache.clear()
    yield
    indices._index_minute_cache.clear()


def _request() -> SimpleNamespace:
    repo = MagicMock()
    repo.get_index_instruments.return_value = pl.DataFrame({
        "symbol": ["000001.SH"],
        "name": ["上证指数"],
    })
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=MagicMock())),
    )


def _bars() -> pl.DataFrame:
    return pl.DataFrame({"datetime": ["2026-09-12 09:35:00"], "close": [3000.0]})


def test_past_date_requests_provider():
    """历史深度由分钟源决定, API 不应直接拒绝所有历史请求。"""
    indices._index_minute_cache.clear()
    past = indices.cn_today() - timedelta(days=1)
    with patch.object(indices.kline_sync, "fetch_minute_single", return_value=_bars()) as fetch:
        result = indices.get_index_minute(_request(), symbol="000001.SH", trade_date=past)
    fetch.assert_called_once()
    assert len(result["rows"]) == 1
    assert result["source"] == "live"
    assert result["date"] == past.isoformat()
    assert result["name"] == "上证指数"
    indices._index_minute_cache.clear()


def test_today_result_cached_within_ttl():
    """当日请求走数据源, 10s 内同代码同日的重复轮询命中缓存只打一次数据源。"""
    indices._index_minute_cache.clear()
    with patch.object(indices.kline_sync, "fetch_minute_single", return_value=_bars()) as fetch:
        first = indices.get_index_minute(_request(), symbol="000001.SH", trade_date=indices.cn_today())
        second = indices.get_index_minute(_request(), symbol="000001.SH", trade_date=indices.cn_today())
    fetch.assert_called_once()
    assert first["source"] == "live"
    assert len(first["rows"]) == 1
    assert second["rows"] == first["rows"]
    assert second["source"] == "live"
    indices._index_minute_cache.clear()
