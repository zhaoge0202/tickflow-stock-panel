"""逐笔成交 API 入参模型测试。"""
from __future__ import annotations

import datetime as dt

from app.api.trade_ticks import PersistReq


def test_persist_req_accepts_date_field():
    req = PersistReq(symbol="000001.SZ", date="2026-07-07")

    assert req.symbol == "000001.SZ"
    assert req.date == dt.date(2026, 7, 7)
