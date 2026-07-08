from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services import alert_outcome, intraday_replay, quote_tick_store, signal_frame

CN = ZoneInfo("Asia/Shanghai")


def _ms(day: int, hour: int, minute: int) -> int:
    return int(datetime(2026, 7, day, hour, minute, tzinfo=CN).timestamp() * 1000)


def test_signal_frame_exposes_minute_and_trade_summary(monkeypatch, tmp_path):
    rows = [
        {"symbol": "002491.SZ", "last_price": 10.0, "prev_close": 9.8, "open": 10.0, "high": 10.0, "low": 9.9, "volume": 100, "amount": 100_000, "timestamp": _ms(8, 9, 30)},
        {"symbol": "002491.SZ", "last_price": 10.1, "prev_close": 9.8, "open": 10.0, "high": 10.1, "low": 9.9, "volume": 120, "amount": 120_000, "timestamp": _ms(8, 9, 31)},
        {"symbol": "002491.SZ", "last_price": 10.3, "prev_close": 9.8, "open": 10.0, "high": 10.3, "low": 9.9, "volume": 180, "amount": 180_000, "timestamp": _ms(8, 9, 36)},
    ]
    quote_tick_store.append_many(tmp_path, rows, source="tdxapi", force_flush=True)
    calls = 0

    def fake_trade_tick_summary(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "tick_buy_amount": 200_000,
            "tick_sell_amount": 50_000,
            "tick_net_amount": 150_000,
            "large_buy_amount": 200_000,
            "large_sell_amount": 0,
            "aggressive_buy_ratio": 0.8,
            "large_order_count": 1,
            "tick_sample_count": 2,
        }

    monkeypatch.setattr(signal_frame, "_trade_tick_summary", fake_trade_tick_summary)

    frame = signal_frame.build_detail(tmp_path, None, "002491.SZ", target_date=datetime(2026, 7, 8, tzinfo=CN).date())

    assert frame is not None
    assert frame["ret_5m"] > 0
    assert frame["amount_5m"] >= 0
    assert frame["tick_net_amount"] == 150_000
    assert "aggressive_buy_ratio_high" in frame["active_signals"]
    assert "large_order_net_inflow" in frame["active_signals"]
    assert calls == 1


def test_intraday_replay_uses_signal_frame_rules_and_outputs_returns(tmp_path):
    quote_tick_store.append_many(tmp_path, [
        {"symbol": "002491.SZ", "last_price": 10.0, "prev_close": 9.8, "open": 10.0, "high": 10.0, "low": 9.9, "volume": 100, "amount": 100_000, "timestamp": _ms(8, 9, 30)},
        {"symbol": "002491.SZ", "last_price": 10.4, "prev_close": 9.8, "open": 10.0, "high": 10.4, "low": 9.9, "volume": 150, "amount": 110_000, "timestamp": _ms(8, 9, 31)},
        {"symbol": "002491.SZ", "last_price": 10.7, "prev_close": 9.8, "open": 10.0, "high": 10.7, "low": 9.9, "volume": 200, "amount": 120_000, "timestamp": _ms(8, 9, 36)},
    ], source="tdxapi", force_flush=True)

    result = intraday_replay.run_replay(
        tmp_path,
        target_date=datetime(2026, 7, 8, tzinfo=CN).date(),
        symbols=["002491"],
        start_time="09:30",
        end_time="10:00",
    )

    assert result["status"] == "succeeded"
    assert result["requested_symbols"] == ["002491"]
    assert result["symbols"] == ["002491.SZ"]
    assert result["tick_count"] == 3
    assert result["window_tick_count"] == 3
    assert result["rule_count"] >= 1
    assert result["triggered"] >= 1
    assert result["events"][0]["rule_id"].startswith("replay_")
    assert "returns" in result["events"][0]
    assert result["rule_summary"]


def test_intraday_replay_falls_back_to_trade_ticks_when_quote_ticks_outside_window(monkeypatch, tmp_path):
    quote_tick_store.append_many(tmp_path, [
        {"symbol": "000725.SZ", "last_price": 7.63, "timestamp": _ms(8, 15, 33)},
    ], source="tdxapi", force_flush=True)

    class FakeTDXAPIProvider:
        def get_trade_ticks(self, symbol, trade_date, mode="all", limit=None):
            assert symbol == "000725.SZ"
            assert trade_date == datetime(2026, 7, 8, tzinfo=CN).date()
            assert mode == "all"
            assert limit is None
            return [
                {
                    "symbol": symbol,
                    "datetime": datetime(2026, 7, 8, 9, 30),
                    "seq_in_day": 1,
                    "price": 7.69,
                    "volume": 100,
                    "amount": 76_900,
                    "side": "sell",
                    "side_label": "主卖",
                },
                {
                    "symbol": symbol,
                    "datetime": datetime(2026, 7, 8, 9, 30),
                    "seq_in_day": 2,
                    "price": 7.7,
                    "volume": 50,
                    "amount": 38_500,
                    "side": "buy",
                    "side_label": "主买",
                },
                {
                    "symbol": symbol,
                    "datetime": datetime(2026, 7, 8, 9, 31),
                    "seq_in_day": 3,
                    "price": 7.73,
                    "volume": 10,
                    "amount": 7_730,
                    "side": "buy",
                    "side_label": "主买",
                },
            ]

        def close(self):
            pass

    monkeypatch.setattr("app.plugins.tdxapi.provider.TDXAPIProvider", FakeTDXAPIProvider)

    target_date = datetime(2026, 7, 8, tzinfo=CN).date()
    loaded = intraday_replay._load_replay_ticks(
        tmp_path,
        target_date=target_date,
        symbols=["000725.SZ"],
        start_time="09:30",
        end_time="15:00",
    )
    result = intraday_replay.run_replay(
        tmp_path,
        target_date=target_date,
        symbols=["000725"],
        start_time="09:30",
        end_time="15:00",
    )

    assert loaded["tick_source"] == "tdxapi_trade_ticks"
    assert loaded["quote_tick_count"] == 1
    assert loaded["quote_window_tick_count"] == 0
    assert loaded["trade_tick_count"] == 3
    assert [row["amount"] for row in loaded["ticks"]] == [76_900, 115_400, 123_130]
    assert [row["volume"] for row in loaded["ticks"]] == [100, 150, 160]
    assert result["tick_source"] == "tdxapi_trade_ticks"
    assert result["requested_symbols"] == ["000725"]
    assert result["symbols"] == ["000725.SZ"]
    assert result["tick_count"] == 3
    assert result["window_tick_count"] == 3
    assert result["quote_tick_count"] == 1
    assert result["quote_window_tick_count"] == 0
    assert result["trade_tick_count"] == 3
    assert result["trade_window_tick_count"] == 3
    assert result["tick_time_range"]["start"].startswith("2026-07-08T09:30:00")
    assert result["window_time_range"]["end"].startswith("2026-07-08T09:31:00")


def test_alert_outcome_uses_trigger_date_and_adds_close_next_day(monkeypatch, tmp_path):
    event = {
        "ts": _ms(8, 9, 30),
        "source": "price",
        "rule_id": "r1",
        "type": "price",
        "symbol": "002491.SZ",
        "price": 10.0,
        "signals": ["vwap_breakout"],
        "message": "测试提醒",
    }
    monkeypatch.setattr(alert_outcome.alert_store, "list_recent", lambda *args, **kwargs: [event])
    quote_tick_store.append_many(tmp_path, [
        {"symbol": "002491.SZ", "last_price": 10.0, "timestamp": _ms(8, 9, 30)},
        {"symbol": "002491.SZ", "last_price": 10.5, "timestamp": _ms(8, 9, 35)},
        {"symbol": "002491.SZ", "last_price": 10.8, "timestamp": _ms(8, 10, 30)},
        {"symbol": "002491.SZ", "last_price": 11.0, "timestamp": _ms(9, 9, 30)},
    ], source="tdxapi", force_flush=True)

    rows = alert_outcome.track_pending(tmp_path)
    outcome = rows[0]

    assert outcome["returns"]["5m"] == pytest.approx(0.05)
    assert outcome["returns"]["close"] == pytest.approx(0.08)
    assert outcome["returns"]["next_day"] == pytest.approx(0.1)
    assert outcome["status"] == "closed"
