"""逐笔成交 MySQL 存储辅助函数测试。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.trade_tick_mysql import _from_mysql_row, _to_mysql_row, parse_mysql_url


def test_parse_mysql_url():
    cfg = parse_mysql_url("mysql+pymysql://demo_user:demo_pass@127.0.0.1:3306/tickflow_stock_panel?charset=utf8mb4")

    assert cfg.host == "127.0.0.1"
    assert cfg.port == 3306
    assert cfg.user == "demo_user"
    assert cfg.password == "demo_pass"
    assert cfg.database == "tickflow_stock_panel"
    assert cfg.charset == "utf8mb4"


def test_to_mysql_row_converts_trade_tick_payload():
    row = _to_mysql_row({
        "symbol": "000001.SZ",
        "trade_date": dt.date(2026, 7, 7),
        "datetime": dt.datetime(2026, 7, 7, 13, 21),
        "seq_in_day": 7,
        "price": 10.45,
        "volume": 3,
        "amount": 3135,
        "side": "sell",
        "side_label": "主卖",
        "order_count": 1,
        "raw_status": 1,
        "source": "tdxapi",
    })

    assert row["trade_date"] == dt.date(2026, 7, 7)
    assert row["trade_time"] == dt.datetime(2026, 7, 7, 13, 21)
    assert row["price"] == Decimal("10.45")
    assert row["amount"] == Decimal("3135")


def test_from_mysql_row_returns_json_ready_values():
    row = _from_mysql_row({
        "symbol": "000001.SZ",
        "trade_date": dt.date(2026, 7, 7),
        "trade_time": dt.datetime(2026, 7, 7, 13, 21),
        "seq_in_day": 7,
        "price": Decimal("10.4500"),
        "volume": 3,
        "amount": Decimal("3135.0000"),
        "side": "sell",
        "side_label": "主卖",
        "order_count": 1,
        "raw_status": 1,
        "source": "tdxapi",
        "ingested_at": None,
        "updated_at": None,
    })

    assert row["trade_date"] == "2026-07-07"
    assert row["datetime"] == "2026-07-07T13:21:00"
    assert row["price"] == 10.45
    assert row["amount"] == 3135.0
