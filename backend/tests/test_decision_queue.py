from __future__ import annotations

import json
from datetime import date, datetime
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
TRADE_DATE = date(2026, 7, 8)


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

    queue = decision_queue.build_queue(tmp_path, FakeRepo(), target_date=TRADE_DATE)

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

    summary = decision_queue.summary(tmp_path, target_date=TRADE_DATE)

    assert summary["total"] == 0
    assert summary["pending"] == 0
    assert summary["quality"]["source"] == "tdxapi"
    assert summary["quality"]["missing_symbols"] == []
    assert summary["quality"]["stale_symbols"] == []


def test_record_action_does_not_rebuild_detail(monkeypatch, tmp_path):
    def fail_get_item(*args, **kwargs):
        raise AssertionError("record_action 不应同步重建详情")

    monkeypatch.setattr(decision_queue, "get_item", fail_get_item)

    out = decision_queue.record_action(
        tmp_path,
        FakeRepo(),
        "002491.SZ",
        {"action": "mark_wait", "note": "性能回归测试"},
    )

    assert out["ok"] is True
    assert out["event"]["symbol"] == "002491.SZ"
    assert out["event"]["status"] == "waiting"
    assert "item" not in out


def test_get_item_does_not_build_full_queue(monkeypatch, tmp_path):
    def fail_build_queue(*args, **kwargs):
        raise AssertionError("get_item 不应先构建全量队列")

    monkeypatch.setattr(decision_queue, "build_queue", fail_build_queue)
    monkeypatch.setattr(decision_queue.signal_frame, "build_detail", lambda *args, **kwargs: None)
    manual_positions.save_one(tmp_path, {
        "symbol": "589020",
        "shares": 600,
        "cost_price": 3.35,
    }, FakeRepo())

    item = decision_queue.get_item(tmp_path, FakeRepo(), "589020")

    assert item is not None
    assert item["symbol"] == "589020.SH"
    assert item["position"]["symbol"] == "589020.SH"
    assert item["signal_frame"] is None


def test_decision_queue_normalizes_legacy_bare_etf_position(tmp_path):
    manual_positions.path(tmp_path).write_text(
        json.dumps({
            "positions": [
                {
                    "symbol": "589020",
                    "shares": 600,
                    "cost_price": 3.35,
                }
            ]
        }),
        encoding="utf-8",
    )
    quote_tick_store.append_many(tmp_path, [{
        "symbol": "589020.SH",
        "name": "科创半导体设备ETF鹏华",
        "last_price": 3.42,
        "prev_close": 3.38,
        "volume": 100,
        "amount": 342_000,
        "timestamp": _ms(10, 5),
        "bid1_price": 3.41,
        "bid1_vol": 8000,
        "ask1_price": 3.42,
        "ask1_vol": 1000,
        "bid_depth_amount": 2_728_000,
        "ask_depth_amount": 342_000,
        "depth_imbalance": 0.777,
        "outside_volume": 1200,
        "inside_volume": 500,
        "outside_inside_ratio": 2.4,
    }], source="tdxapi", force_flush=True)

    queue = decision_queue.build_queue(tmp_path, FakeRepo(), target_date=TRADE_DATE)

    assert queue["total"] == 1
    assert queue["quality"]["missing_symbols"] == []
    item = queue["items"][0]
    assert item["symbol"] == "589020.SH"
    assert item["latest_price"] == 3.42
    assert item["position"]["symbol"] == "589020.SH"
    assert "depth_bid_dominant" in item["signals"]
    assert item["signal_frame"]["microstructure"]["depth_imbalance"] == 0.777

    detail = decision_queue.get_item(tmp_path, FakeRepo(), "589020", target_date=TRADE_DATE)

    assert detail is not None
    assert detail["symbol"] == "589020.SH"
    assert "买盘厚度占优" in detail["reasons"][0]


def test_decision_queue_marks_chasing_as_risk_when_market_breadth_is_weak(monkeypatch, tmp_path):
    monkeypatch.setattr(decision_queue.signal_frame.market_breadth, "cached", lambda data_dir: {
        "source": "tdxapi",
        "status": "ready",
        "event_ts": _ms(9, 31),
        "ingest_ts": _ms(9, 31),
        "up_count": 600,
        "down_count": 2600,
        "flat_count": 120,
        "total_count": 3320,
        "up_down_ratio": 600 / 2600,
        "market_temperature": "cold",
        "major_index_change_pct": -0.018,
    })
    quote_tick_store.append_many(tmp_path, [
        {
            "symbol": "002491.SZ",
            "name": "测试股",
            "last_price": 10.0,
            "prev_close": 9.8,
            "open": 10.0,
            "high": 10.0,
            "low": 9.9,
            "volume": 100,
            "amount": 100_000,
            "timestamp": _ms(9, 30),
        },
        {
            "symbol": "002491.SZ",
            "name": "测试股",
            "last_price": 10.2,
            "prev_close": 9.8,
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "volume": 180,
            "amount": 500_000,
            "timestamp": _ms(9, 31),
            "speed_rate": 0.8,
        },
    ], source="tdxapi", force_flush=True)
    manual_positions.save_one(tmp_path, {
        "symbol": "002491.SZ",
        "shares": 1000,
        "cost_price": 9.8,
    })

    queue = decision_queue.build_queue(tmp_path, FakeRepo(), target_date=TRADE_DATE)

    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["side"] == "risk"
    assert "speed_up" in item["signals"]
    assert "market_breadth_weak" in item["risk_flags"]
    assert item["signal_frame"]["market_context"]["up_down_ratio"] == 600 / 2600
    assert any("市场广度偏弱" in reason for reason in item["reasons"])
