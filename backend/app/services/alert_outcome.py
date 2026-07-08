"""告警后验收益追踪。

不改写 alerts.jsonl, 以 alert_key 关联独立 outcome 文件。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services import alert_store, quote_tick_store

logger = logging.getLogger(__name__)

WINDOWS = {"5m": 5 * 60_000, "15m": 15 * 60_000, "30m": 30 * 60_000, "60m": 60 * 60_000}
CN_TZ = ZoneInfo("Asia/Shanghai")
_lock = threading.Lock()


class AlertOutcomeTracker:
    """轻量后台追踪器。失败静默降级, 不阻塞行情链路。"""

    def __init__(self, data_dir: Path, interval_s: float = 30.0) -> None:
        self.data_dir = data_dir
        self.interval_s = interval_s
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="alert-outcome-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                track_pending(self.data_dir)
            except Exception as e:
                logger.debug("alert outcome tracker skipped: %s", e)
            waited = 0.0
            while self._running and waited < self.interval_s:
                time.sleep(0.5)
                waited += 0.5


def path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "alert_outcomes.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def track_pending(data_dir: Path, *, days: int = 7) -> list[dict]:
    alerts = alert_store.list_recent(data_dir, days=days, limit=5000)
    outcomes = {row["alert_key"]: row for row in read_all(data_dir)}
    updated = []
    for ev in alerts:
        symbol = str(ev.get("symbol") or "").strip().upper()
        if not symbol or not ev.get("ts"):
            continue
        key = alert_key(ev)
        current = outcomes.get(key, _base_outcome(ev, key))
        row = _compute(data_dir, ev, current)
        outcomes[key] = row
        updated.append(row)
    _write_all(data_dir, list(outcomes.values()))
    return updated


def list_recent(data_dir: Path, *, days: int = 7, strategy_id: str | None = None) -> list[dict]:
    cutoff = int((time.time() - days * 86400) * 1000)
    rows = [r for r in read_all(data_dir) if int(r.get("trigger_ts") or 0) >= cutoff]
    if strategy_id:
        rows = [r for r in rows if r.get("strategy_id") == strategy_id or r.get("rule_id") == strategy_id]
    rows.sort(key=lambda r: r.get("trigger_ts") or 0, reverse=True)
    return rows


def summary(data_dir: Path, *, group_by: str = "signal", days: int = 7) -> dict:
    rows = list_recent(data_dir, days=days)
    groups: dict[str, list[dict]] = {}
    for row in rows:
        keys = row.get("signals") or ["unknown"]
        if group_by not in {"signal", "symbol", "strategy"}:
            key_list = ["all"]
        elif group_by == "symbol":
            key_list = [row.get("symbol") or "unknown"]
        elif group_by == "strategy":
            key_list = [row.get("strategy_id") or row.get("rule_id") or "unknown"]
        else:
            key_list = keys
        for key in key_list:
            groups.setdefault(str(key), []).append(row)
    out = []
    for key, items in groups.items():
        vals_15m = [_num(item.get("returns", {}).get("15m")) for item in items]
        vals_15m = [v for v in vals_15m if v is not None]
        close_vals = [_num(item.get("returns", {}).get("close")) for item in items]
        close_vals = [v for v in close_vals if v is not None]
        out.append({
            "key": key,
            "count": len(items),
            "hit_rate_15m": sum(1 for v in vals_15m if v > 0) / len(vals_15m) if vals_15m else None,
            "avg_return_15m": sum(vals_15m) / len(vals_15m) if vals_15m else None,
            "avg_return_close": sum(close_vals) / len(close_vals) if close_vals else None,
            "avg_mfe": _avg([_num(item.get("mfe")) for item in items]),
            "avg_mae": _avg([_num(item.get("mae")) for item in items]),
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return {"group_by": group_by, "items": out, "total": len(rows)}


def read_all(data_dir: Path) -> list[dict]:
    p = path(data_dir)
    if not p.exists():
        return []
    rows = []
    try:
        with _lock, p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        logger.warning("alert_outcomes read failed: %s", e)
    return rows


def alert_key(ev: dict) -> str:
    return ":".join([
        str(ev.get("source") or ""),
        str(ev.get("rule_id") or ev.get("type") or ""),
        str(ev.get("symbol") or ""),
        str(ev.get("ts") or ""),
    ])


def _compute(data_dir: Path, ev: dict, current: dict) -> dict:
    symbol = str(ev.get("symbol") or "").upper()
    trigger_ts = int(ev.get("ts") or 0)
    trigger_price = _num(ev.get("price"))
    trigger_date = datetime.fromtimestamp(trigger_ts / 1000, tz=CN_TZ).date()
    ticks = quote_tick_store.read_ticks(data_dir, target_date=trigger_date, symbols=[symbol])
    future = [r for r in ticks if int(r.get("event_ts") or r.get("ingest_ts") or 0) >= trigger_ts]
    if trigger_price is None and future:
        trigger_price = _num(future[0].get("last_price"))
    returns = dict(current.get("returns") or {})
    prices = []
    for row in future:
        price = _num(row.get("last_price"))
        ts = int(row.get("event_ts") or row.get("ingest_ts") or 0)
        if price is not None:
            prices.append((ts, price))
    if trigger_price:
        for name, delta in WINDOWS.items():
            if name not in returns:
                returns[name] = _return_at(prices, trigger_ts + delta, trigger_price)
        if "close" not in returns:
            returns["close"] = _close_return(prices, trigger_price)
        if "next_day" not in returns:
            next_ticks = quote_tick_store.read_ticks(
                data_dir,
                target_date=trigger_date + timedelta(days=1),
                symbols=[symbol],
            )
            next_price = _first_price(next_ticks)
            returns["next_day"] = (next_price - trigger_price) / trigger_price if next_price else None
        if prices:
            px = [p for _, p in prices]
            current["mfe"] = (max(px) - trigger_price) / trigger_price
            current["mae"] = (min(px) - trigger_price) / trigger_price
    current["trigger_price"] = trigger_price
    current["returns"] = returns
    required = [*WINDOWS.keys(), "close", "next_day"]
    if not prices:
        current["status"] = "insufficient_data"
    elif all(returns.get(k) is not None for k in required):
        current["status"] = "closed"
    else:
        current["status"] = "tracking"
    current["updated_at"] = int(time.time() * 1000)
    return current


def _return_at(prices: list[tuple[int, float]], target_ts: int, trigger_price: float) -> float | None:
    for ts, price in prices:
        if ts >= target_ts:
            return (price - trigger_price) / trigger_price
    return None


def _close_return(prices: list[tuple[int, float]], trigger_price: float) -> float | None:
    if not prices:
        return None
    return (prices[-1][1] - trigger_price) / trigger_price


def _first_price(rows: list[dict]) -> float | None:
    for row in sorted(rows, key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0)):
        price = _num(row.get("last_price"))
        if price is not None:
            return price
    return None


def _base_outcome(ev: dict, key: str) -> dict:
    return {
        "alert_key": key,
        "trigger_ts": ev.get("ts"),
        "symbol": ev.get("symbol"),
        "name": ev.get("name"),
        "source": ev.get("source"),
        "type": ev.get("type"),
        "rule_id": ev.get("rule_id"),
        "strategy_id": ev.get("strategy_id"),
        "signals": ev.get("signals") or [],
        "message": ev.get("message"),
        "status": "pending",
        "returns": {},
        "mfe": None,
        "mae": None,
    }


def _write_all(data_dir: Path, rows: list[dict]) -> None:
    rows.sort(key=lambda r: r.get("trigger_ts") or 0)
    with _lock, path(data_dir).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None
