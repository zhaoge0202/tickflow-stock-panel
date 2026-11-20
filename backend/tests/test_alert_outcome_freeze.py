"""告警后验冻结: 超过 24h 的 outcome 不再重算, 无进展不重写文件。"""
from __future__ import annotations

import time

from app.services import alert_outcome, alert_store


def _ev(ts: int, symbol: str = "002272.SZ") -> dict:
    return {
        "ts": ts,
        "source": "strategy",
        "type": "buy_signal",
        "rule_id": "r1",
        "strategy_id": "s1",
        "symbol": symbol,
        "name": "川润股份",
        "price": 12.2,
    }


def test_frozen_after_24h() -> None:
    now = int(time.time() * 1000)
    assert alert_outcome._frozen({"status": "tracking"}, _ev(now - 25 * 3600_000), now) is True
    assert alert_outcome._frozen({"status": "tracking"}, _ev(now - 23 * 3600_000), now) is False
    assert alert_outcome._frozen({"status": "closed"}, _ev(now - 60_000), now) is True


def test_old_outcome_not_recomputed_and_no_rewrite(tmp_path, monkeypatch) -> None:
    now = int(time.time() * 1000)
    old = _ev(now - 3 * 86400_000, "002272.SZ")
    fresh = _ev(now - 5 * 60_000, "603496.SH")
    alert_store.append(tmp_path, old)
    alert_store.append(tmp_path, fresh)

    # 预置旧告警的 outcome (仍在 tracking)
    key_old = alert_outcome.alert_key(old)
    alert_outcome._write_all(tmp_path, [{
        "alert_key": key_old, "trigger_ts": old["ts"], "symbol": old["symbol"],
        "status": "tracking", "returns": {},
    }])

    computed: list[int] = []
    writes: list[int] = []

    def fake_compute(data_dir, ev, current):
        computed.append(int(ev["ts"]))
        return {**current, "status": "tracking"}

    monkeypatch.setattr(alert_outcome, "_compute", fake_compute)
    monkeypatch.setattr(alert_outcome, "_write_all", lambda d, rows: writes.append(len(rows)))

    alert_outcome.track_pending(tmp_path)
    assert old["ts"] not in computed          # 冻结: 不重算
    assert fresh["ts"] in computed            # 新告警照常计算
    assert writes and writes[0] >= 2          # 有新结果才重写
