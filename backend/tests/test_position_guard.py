"""持仓守护测试: 登记/止损/破位/落袋/持久化/回填。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import position_guard


@pytest.fixture(autouse=True)
def _clean():
    position_guard.reset_state()
    yield
    position_guard.reset_state()


def _frame(symbol: str, price: float, ma5: float | None) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "close": [price],
        "ma5": [ma5],
        "change_pct": [0.0],
    })


def _register(data_dir: Path, price: float = 10.0, days_ago: int = 1) -> None:
    ts = int((datetime.now() - timedelta(days=days_ago)).timestamp() * 1000)
    position_guard.register(
        data_dir, strategy_id="custom_dual_edge_focus", symbol="002272.SZ",
        name="川润股份", price=price, ts=ts,
    )


def test_register_dedupes_by_symbol(tmp_path):
    _register(tmp_path, 10.0, days_ago=1)
    _register(tmp_path, 10.2, days_ago=1)          # 同日同票只更新价格
    # Focus 与 Apex 同时推同一只票: 只保留一条
    position_guard.register(
        tmp_path, strategy_id="custom_dual_edge_apex", symbol="002272.SZ",
        name="川润股份", price=10.1,
        ts=int((datetime.now() - timedelta(days=1)).timestamp() * 1000),
    )
    positions = position_guard.list_positions(tmp_path)
    assert len(positions) == 1
    assert positions[0]["entry_price"] == 10.0     # 同日保留首次价格


def test_stop_loss_fires_once_per_day(tmp_path):
    _register(tmp_path, 10.0)
    events = position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.4, 9.8))
    assert [e["type"] for e in events] == ["guardian_stop"]
    assert "止损" in events[0]["message"]
    # 当日不重复
    assert position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.3, 9.8)) == []


def test_ma5_break_skips_entry_day(tmp_path):
    _register(tmp_path, 10.0, days_ago=0)          # 当天入场
    assert position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.9, 10.1)) == []
    position_guard.reset_state()
    (tmp_path / "user_data" / "position_guard.json").unlink()
    _register(tmp_path, 10.0, days_ago=1)          # 隔日
    events = position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.9, 10.1))
    assert [e["type"] for e in events] == ["guardian_ma5"]


def test_take_profit_hint(tmp_path):
    _register(tmp_path, 10.0)
    events = position_guard.evaluate(tmp_path, _frame("002272.SZ", 10.3, 9.9))
    assert [e["type"] for e in events] == ["guardian_tp"]
    assert "落袋" in events[0]["message"]


def test_state_survives_process_restart(tmp_path):
    _register(tmp_path, 10.0)
    position_guard.reset_state()                   # 模拟重启: 清进程内状态
    events = position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.4, 9.8))
    assert [e["type"] for e in events] == ["guardian_stop"]


def test_positions_expire_after_track_days(tmp_path):
    _register(tmp_path, 10.0, days_ago=position_guard.TRACK_DAYS + 1)
    assert position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.0, 9.0)) == []
    assert position_guard.list_positions(tmp_path) == []


def test_bootstrap_from_alerts_jsonl(tmp_path):
    from app.services import alert_store

    alert_store.append(tmp_path, {
        "ts": int(datetime.now().timestamp() * 1000),
        "source": "strategy",
        "type": "buy_signal",
        "strategy_id": "custom_dual_edge_focus",
        "symbol": "603496.SH",
        "name": "恒为科技",
        "price": 21.0,
    })
    added = position_guard.bootstrap_from_alerts(tmp_path)
    assert added == 1
    positions = position_guard.list_positions(tmp_path)
    assert positions[0]["symbol"] == "603496.SH"
    # 回填幂等
    assert position_guard.bootstrap_from_alerts(tmp_path) == 0


def test_one_alert_per_symbol_per_day(tmp_path):
    """一只票一天只发一条: 止损之后当天不再发落袋/破位。"""
    _register(tmp_path, 10.0)
    assert [e["type"] for e in position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.4, 9.8))] == ["guardian_stop"]
    _register(tmp_path, 10.0)                       # 同日再次登记 (模拟重启回填)
    assert position_guard.evaluate(tmp_path, _frame("002272.SZ", 10.5, 9.9)) == []


def test_restart_does_not_refire_from_alert_store(tmp_path):
    """账本从 alerts.jsonl 回灌: 重启后不会把同一条提醒再推一遍。"""
    from app.services import alert_store

    alert_store.append(tmp_path, {
        "ts": int(datetime.now().timestamp() * 1000),
        "source": "guardian",
        "type": "guardian_stop",
        "strategy_id": "custom_dual_edge_focus",
        "symbol": "002272.SZ",
        "name": "川润股份",
        "price": 9.4,
        "message": "触发 -5% 止损",
    })
    _register(tmp_path, 10.0)
    position_guard.reset_state()                    # 模拟重启: 进程内状态清空
    assert position_guard.evaluate(tmp_path, _frame("002272.SZ", 9.3, 9.8)) == []
