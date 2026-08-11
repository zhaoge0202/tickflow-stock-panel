from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services import auction_confirmation, quote_tick_store

CN = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 11)


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 8, 11, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def test_confirm_cached_strategy_results_uses_latest_window_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(auction_confirmation, "_gate_status", lambda *_args: "open")

    cached = {
        "as_of": "2026-08-10",
        "results": {
            "strategy_a": {
                "as_of": "2026-08-10",
                "total": 2,
                "rows": [
                    {"symbol": "000001.SZ", "score": 80.0},
                    {"symbol": "000002.SZ", "score": 70.0},
                ],
            },
        },
        "updated_at": 1,
    }

    quote_tick_store.append_many(tmp_path, [
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "last_price": 9.8,
            "auction_price": 9.8,
            "auction_matched_volume": 800,
            "price_type": "auction_reference",
            "market_phase": "preopen_auction",
            "timestamp": _ms(9, 23, 10),
        },
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "last_price": 10.0,
            "auction_price": 10.0,
            "auction_matched_volume": 1000,
            "auction_unmatched_volume": 0,
            "auction_unmatched_side": "buy",
            "auction_pressure_score": 0.8,
            "price_type": "auction_reference",
            "market_phase": "preopen_auction",
            "timestamp": _ms(9, 24, 30),
        },
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "last_price": 12.0,
            "auction_price": 12.0,
            "auction_matched_volume": 9999,
            "auction_pressure_score": 9.9,
            "price_type": "auction_reference",
            "market_phase": "preopen_auction",
            "timestamp": _ms(9, 25, 0),
        },
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "last_price": 10.15,
            "change_pct": 0.015,
            "volume": 3000,
            "amount": 30_450,
            "timestamp": _ms(9, 25, 0),
        },
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "last_price": 10.4,
            "change_pct": 0.04,
            "volume": 5000,
            "amount": 52_000,
            "timestamp": _ms(9, 29, 59),
        },
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "last_price": 20.0,
            "change_pct": 1.0,
            "volume": 99_999,
            "amount": 1_999_980,
            "timestamp": _ms(9, 30, 0),
        },
        {
            "symbol": "000002.SZ",
            "name": "万科A",
            "last_price": 8.0,
            "auction_price": 8.0,
            "auction_matched_volume": 500,
            "price_type": "auction_reference",
            "market_phase": "preopen_auction",
            "timestamp": _ms(9, 24, 50),
        },
    ], source="tdxapi", force_flush=True)

    payload = auction_confirmation.confirm_cached_strategy_results(
        tmp_path,
        cached,
        as_of=date(2026, 8, 10),
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
    )

    result = payload["results"]["strategy_a"]
    assert payload["gate_status"] == "confirmed"
    assert result["base_total"] == 2
    assert result["confirmed_total"] == 1
    assert result["total"] == 1
    assert [row["symbol"] for row in result["rows"]] == ["000001.SZ"]
    row = result["rows"][0]
    assert row["auction_price"] == 10.0
    assert row["auction_event_time"] == "09:24:30"
    assert row["open_confirm_price"] == 10.4
    assert row["open_confirm_time"] == "09:29:59"
    assert row["open_confirm_vs_auction_pct"] == 0.040000000000000036
    assert result["pending_trade_total"] == 1


def test_confirm_cached_strategy_results_stays_pending_before_gate(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-08-10",
        "results": {
            "strategy_a": {
                "as_of": "2026-08-10",
                "total": 1,
                "rows": [{"symbol": "000001.SZ", "score": 80.0}],
            },
        },
        "updated_at": 1,
    }

    monkeypatch.setattr(auction_confirmation, "_gate_status", lambda *_args: "pending_gate")

    def _fail_read_ticks(*_args, **_kwargs):
        raise AssertionError("09:25 前不应读取 quote_ticks")

    monkeypatch.setattr(quote_tick_store, "read_ticks", _fail_read_ticks)

    payload = auction_confirmation.confirm_cached_strategy_results(
        tmp_path,
        cached,
        as_of=date(2026, 8, 10),
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
    )

    result = payload["results"]["strategy_a"]
    assert payload["gate_status"] == "pending_gate"
    assert result["base_total"] == 1
    assert result["total"] == 0
    assert result["rows"] == []


def test_confirm_cached_strategy_results_reports_stale_as_of_for_wrong_cache_date(monkeypatch, tmp_path):
    monkeypatch.setattr(auction_confirmation, "_gate_status", lambda *_args: "open")
    cached = {
        "as_of": "2026-08-09",
        "results": {
            "strategy_a": {
                "as_of": "2026-08-09",
                "total": 1,
                "rows": [{"symbol": "000001.SZ", "score": 80.0}],
            },
        },
        "updated_at": 1,
    }

    def _fail_read_ticks(*_args, **_kwargs):
        raise AssertionError("缓存日期不一致时不应读取 quote_ticks")

    monkeypatch.setattr(quote_tick_store, "read_ticks", _fail_read_ticks)

    payload = auction_confirmation.confirm_cached_strategy_results(
        tmp_path,
        cached,
        as_of=date(2026, 8, 10),
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
    )

    assert payload["gate_status"] == "stale_as_of"
    assert payload["as_of"] == "2026-08-10"
    assert payload["cache_as_of"] == "2026-08-09"
    assert payload["results"]["strategy_a"]["base_total"] == 0
    assert payload["results"]["strategy_a"]["rows"] == []
