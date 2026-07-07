"""逐笔成交异步入库状态测试。"""
from __future__ import annotations

import datetime as dt

from app.services.trade_tick_ingest import TradeTickIngestor


def test_status_marks_stale_running_task_as_timeout(monkeypatch):
    ingestor = TradeTickIngestor()
    day = dt.date(2026, 7, 7)
    started_at = (dt.datetime.now() - dt.timedelta(seconds=99)).isoformat(timespec="seconds")

    monkeypatch.setattr(
        "app.services.trade_tick_ingest.trade_tick_mysql_store.configured",
        lambda: False,
    )
    monkeypatch.setattr(
        "app.services.trade_tick_ingest.settings.trade_ticks_persist_timeout_seconds",
        30,
        raising=False,
    )

    with ingestor._lock:
        ingestor._pending.add(("000001.SZ", day))
        ingestor._status[("000001.SZ", day)] = {
            "status": "running",
            "symbol": "000001.SZ",
            "date": day.isoformat(),
            "started_at": started_at,
            "error": None,
        }

    status = ingestor.status("000001.SZ", day)

    assert status["status"] == "timeout"
    assert status["elapsed_seconds"] >= 30
    assert status["timeout_seconds"] == 30
    assert "超过 30s" in status["error"]
