"""最新行情快照 MySQL 存储测试。"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app.services.quote_snapshot_mysql import (
    CREATE_TABLE_SQL,
    UPSERT_SQL,
    QuoteSnapshotMySQLStore,
    _from_mysql_row,
    _to_mysql_row,
)


class FakeCursor:
    def __init__(self, *, one=None, many=None) -> None:
        self.one = one
        self.many = many or []
        self.executed: list[tuple[str, object]] = []
        self.executed_many: list[tuple[str, list[dict]]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, params):
        self.executed_many.append((sql, list(params)))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self._cursor


def test_ddl_uses_symbol_primary_key_and_json_payload():
    assert "PRIMARY KEY (symbol)" in CREATE_TABLE_SQL
    assert "payload JSON NOT NULL" in CREATE_TABLE_SQL
    assert "idx_trade_date_event_ts" in CREATE_TABLE_SQL
    assert "GREATEST(event_ts, VALUES(event_ts))" in UPSERT_SQL


def test_to_mysql_row_keeps_complete_normalized_payload():
    row = _to_mysql_row(
        {
            "symbol": "000001.sz",
            "trade_date": "2026-07-17",
            "event_ts": 1_752_723_000_123,
            "source": "tdxapi",
            "last_price": Decimal("10.45"),
            "depth": {"bid": [10.44, float("nan")]},
        }
    )

    payload = json.loads(row["payload"])
    assert row["symbol"] == "000001.SZ"
    assert row["trade_date"] == dt.date(2026, 7, 17)
    assert payload["symbol"] == "000001.SZ"
    assert payload["last_price"] == 10.45
    assert payload["depth"] == {"bid": [10.44, None]}


def test_to_mysql_row_derives_trade_date_from_timestamp():
    row = _to_mysql_row(
        {
            "symbol": "600000.SH",
            "timestamp": dt.datetime(2026, 7, 17, 9, 30),
            "last_price": 12.3,
        }
    )

    assert row["trade_date"] == dt.date(2026, 7, 17)
    assert json.loads(row["payload"])["trade_date"] == "2026-07-17"


def test_to_mysql_row_rejects_missing_event_time():
    with pytest.raises(ValueError, match="event_ts"):
        _to_mysql_row({"symbol": "000001.SZ", "last_price": 10.0})


def test_upsert_deduplicates_symbol_and_keeps_newest(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    store = QuoteSnapshotMySQLStore("mysql://user:pass@localhost/stock")
    monkeypatch.setattr(store, "connect", lambda with_database=True: connection)

    written = store.upsert(
        [
            {"symbol": "000001.SZ", "event_ts": 1_752_723_000_000, "last_price": 10.0},
            {"symbol": "600000.SH", "event_ts": 1_752_723_000_500, "last_price": 12.0},
            {"symbol": "000001.SZ", "event_ts": 1_752_723_001_000, "last_price": 10.2},
        ],
        batch_size=1,
    )

    assert written == 2
    assert len(cursor.executed_many) == 2
    rows = [batch[0] for _, batch in cursor.executed_many]
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["000001.SZ"]["event_ts"] == 1_752_723_001_000
    assert json.loads(by_symbol["000001.SZ"]["payload"])["last_price"] == 10.2


def test_upsert_skips_invalid_record_without_dropping_valid_batch(monkeypatch):
    cursor = FakeCursor()
    store = QuoteSnapshotMySQLStore("mysql://user:pass@localhost/stock")
    monkeypatch.setattr(
        store,
        "connect",
        lambda with_database=True: FakeConnection(cursor),
    )

    written = store.upsert([
        {"symbol": "BAD.SZ", "last_price": 1.0},
        {"symbol": "000001.SZ", "event_ts": 2_000_000_000_000, "last_price": 10.2},
    ])

    assert written == 1
    assert cursor.executed_many[0][1][0]["symbol"] == "000001.SZ"


def test_list_filters_symbols_and_trade_date(monkeypatch):
    cursor = FakeCursor(
        many=[
            {
                "symbol": "000001.SZ",
                "trade_date": dt.date(2026, 7, 17),
                "event_ts": 1_752_723_000_123,
                "source": "tdxapi",
                "payload": '{"last_price":10.45,"name":"平安银行"}',
            }
        ]
    )
    store = QuoteSnapshotMySQLStore("mysql://user:pass@localhost/stock")
    monkeypatch.setattr(store, "connect", lambda with_database=True: FakeConnection(cursor))

    rows = store.list(["600000.sh", "000001.sz"], "2026-07-17")

    sql, params = cursor.executed[0]
    assert "symbol IN (%s, %s)" in sql
    assert "trade_date = %s" in sql
    assert params == ["000001.SZ", "600000.SH", dt.date(2026, 7, 17)]
    assert rows == [
        {
            "last_price": 10.45,
            "name": "平安银行",
            "symbol": "000001.SZ",
            "trade_date": "2026-07-17",
            "event_ts": 1_752_723_000_123,
            "source": "tdxapi",
        }
    ]


def test_from_mysql_row_accepts_decoded_json_payload():
    row = _from_mysql_row(
        {
            "symbol": "000001.SZ",
            "trade_date": dt.date(2026, 7, 17),
            "event_ts": 123,
            "source": "tdxapi",
            "payload": {"last_price": 10.45},
        }
    )

    assert row["last_price"] == 10.45
    assert row["trade_date"] == "2026-07-17"


def test_ensure_schema_and_health_use_fake_connection(monkeypatch):
    schema_cursor = FakeCursor()
    health_cursor = FakeCursor(one={"n": 1})
    store = QuoteSnapshotMySQLStore("mysql://user:pass@localhost/stock")
    connections = iter([FakeConnection(schema_cursor), FakeConnection(health_cursor)])
    monkeypatch.setattr(store, "connect", lambda with_database=True: next(connections))

    assert store.ensure_schema() == {"database": "stock", "table": "quote_latest"}
    health = store.health()

    assert schema_cursor.executed == [(CREATE_TABLE_SQL, None)]
    assert health["ok"] is True
    assert health["table_ready"] is True
    assert health["table"] == "quote_latest"


def test_health_does_not_connect_when_url_is_empty(monkeypatch):
    store = QuoteSnapshotMySQLStore("")
    monkeypatch.setattr(
        store,
        "connect",
        lambda with_database=True: pytest.fail("不应连接数据库"),
    )

    assert store.health() == {
        "configured": False,
        "enabled": False,
        "ok": False,
        "table_ready": False,
        "message": "未配置 MySQL URL",
    }
