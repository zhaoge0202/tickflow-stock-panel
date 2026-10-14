from __future__ import annotations

from app.services import strategy_purchase_marks


def test_purchase_mark_upsert_and_delete(tmp_path):
    mark = strategy_purchase_marks.save_one(tmp_path, {
        "strategy_id": "custom_oscillation_reversal",
        "strategy_name": "震荡超跌反转",
        "symbol": "000001.sz",
        "signal_date": "2026-08-31",
        "signal_price": 10.2,
        "signal_score": 88,
        "signal_change_pct": 0.03,
    })

    assert mark["symbol"] == "000001.SZ"
    assert len(strategy_purchase_marks.load_all(tmp_path)) == 1

    updated = strategy_purchase_marks.save_one(tmp_path, {
        **mark,
        "signal_price": 10.3,
    })
    assert updated["signal_price"] == 10.3
    assert len(strategy_purchase_marks.load_all(tmp_path)) == 1

    assert strategy_purchase_marks.delete_one(
        tmp_path,
        "custom_oscillation_reversal",
        "000001.SZ",
        "2026-08-31",
    ) is True
    assert strategy_purchase_marks.load_all(tmp_path) == []
