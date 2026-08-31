from __future__ import annotations

from pathlib import Path

from app.services import strategy_history


def test_selection_and_auction_outcome_are_persisted_and_deduplicated(tmp_path: Path):
    rows = [{
        "symbol": "002758.SZ",
        "name": "浙农股份",
        "close": 9.11,
        "change_pct": 0.07,
        "score": 88.0,
    }]

    assert strategy_history.record_selection_snapshot(
        tmp_path,
        strategy_id="custom_dual_edge_focus",
        strategy_name="双刃合-Focus",
        signal_date="2026-08-28",
        trade_date="2026-08-31",
        rows=rows,
    ) == 1
    rejected = {
        "event_key": "auction:auction_rejected:custom_dual_edge_focus:002758.SZ:2026-08-28:2026-08-31",
        "event_type": "auction_rejected",
        "status": "rejected",
        "strategy_id": "custom_dual_edge_focus",
        "strategy_name": "双刃合-Focus",
        "symbol": "002758.SZ",
        "name": "浙农股份",
        "signal_date": "2026-08-28",
        "trade_date": "2026-08-31",
        "phase": "open_confirm",
        "price": 8.95,
        "reason_code": "auction_gap_failed",
        "reason": "竞价开盘 -1.76%，低于最低高开 2.0%",
        "metadata": {"base_price": 9.11},
    }

    assert strategy_history.record_auction_outcomes(tmp_path, [rejected, rejected]) == 1
    events = strategy_history.list_events(
        tmp_path,
        strategy_id="custom_dual_edge_focus",
        symbol="002758.SZ",
        trade_date="2026-08-31",
    )

    assert [event["event_type"] for event in events] == ["auction_rejected", "selected"]
    assert events[0]["reason_code"] == "auction_gap_failed"
    assert events[0]["reason"]
    assert events[0]["strategy_name"] == "双刃合-Focus"


def test_preselect_is_marked_watch_only(tmp_path: Path):
    assert strategy_history.record_selection_snapshot(
        tmp_path,
        strategy_id="custom_dual_edge_focus",
        strategy_name="双刃合-Focus",
        signal_date="2026-08-28",
        trade_date="2026-08-31",
        mode="preselect",
        rows=[{"symbol": "002758.SZ", "close": 9.11}],
    ) == 1

    event = strategy_history.list_events(tmp_path, event_type="preselect")[0]
    assert event["status"] == "watch_only"
    assert "仅供观察" in event["reason"]
