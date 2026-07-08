from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import polars as pl

from app.services import (
    alert_store,
    decision_journal,
    decision_queue,
    manual_positions,
    quote_tick_store,
)

CN = ZoneInfo("Asia/Shanghai")


class FakeRepo:
    def get_enriched_latest_asset(self, asset_type: str):
        return pl.DataFrame(), None

    def get_enriched_latest(self):
        return pl.DataFrame(), None

    def get_name_map(self, symbols):
        return {symbol: "测试股" for symbol in symbols}

    def resolve_asset_type(self, symbol: str):
        return "stock"

    def get_daily_asset(self, asset_type: str, symbol: str, start, end):
        return pl.DataFrame()


def _ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 7, 8, hour, minute, tzinfo=CN).timestamp() * 1000)


def test_decision_queue_merges_alerts_position_and_journal(tmp_path):
    alert_store.append_many(tmp_path, [
        {
            "ts": _ms(9, 45),
            "rule_id": "r1",
            "rule_name": "跌破止损",
            "source": "price",
            "type": "price",
            "symbol": "002491.SZ",
            "name": "测试股",
            "message": "跌破手动止损",
            "price": 9.8,
            "change_pct": -0.02,
            "signals": [],
            "severity": "critical",
        },
        {
            "ts": _ms(9, 46),
            "rule_id": "r2",
            "rule_name": "策略进入",
            "source": "strategy",
            "type": "new_entry",
            "symbol": "002491.SZ",
            "name": "测试股",
            "message": "策略进入观察",
            "price": 9.8,
            "change_pct": -0.02,
            "signals": ["signal_volume_surge"],
            "severity": "info",
        },
    ])
    manual_positions.save_one(tmp_path, {
        "symbol": "002491.SZ",
        "shares": 1000,
        "cost_price": 10.5,
        "stop_loss_price": 10.0,
    })
    quote_tick_store.append_many(tmp_path, [{
        "symbol": "002491.SZ",
        "name": "测试股",
        "last_price": 9.8,
        "prev_close": 10.0,
        "open": 10.2,
        "high": 10.3,
        "low": 9.8,
        "volume": 100,
        "amount": 98_000,
        "timestamp": _ms(9, 46),
    }], source="tdxapi", force_flush=True)
    decision_journal.append_action(tmp_path, {
        "ts": _ms(9, 47),
        "symbol": "002491.SZ",
        "action": "mark_wait",
        "note": "等券商软件确认",
    })

    queue = decision_queue.build_queue(tmp_path, FakeRepo())

    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["symbol"] == "002491.SZ"
    assert item["status"] == "waiting"
    assert item["side"] == "sell_risk"
    assert item["alert_count"] == 2
    assert item["position"]["risk_level"] == "critical"
    assert "跌破手动止损" in item["reasons"]


def test_decision_summary_does_not_report_all_market_stale_without_items(tmp_path):
    quote_tick_store.append_many(tmp_path, [{
        "symbol": "000725.SZ",
        "last_price": 4.2,
        "timestamp": _ms(9, 45),
    }], source="tdxapi", force_flush=True)

    summary = decision_queue.summary(tmp_path)

    assert summary["total"] == 0
    assert summary["pending"] == 0
    assert summary["quality"]["source"] == "tdxapi"
    assert summary["quality"]["missing_symbols"] == []
    assert summary["quality"]["stale_symbols"] == []
