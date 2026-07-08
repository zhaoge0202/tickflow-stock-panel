"""人工决策日志。

只记录用户在决策台里的人工处理状态, 不代表真实委托。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

from app.market_time import cn_today

logger = logging.getLogger(__name__)

_lock = threading.Lock()
VALID_ACTIONS = {"mark_wait", "mark_plan", "mark_manual_done", "mark_ignore", "note", "position_update"}
STATUS_BY_ACTION = {
    "mark_wait": "waiting",
    "mark_plan": "planned",
    "mark_manual_done": "manual_done",
    "mark_ignore": "ignored",
}


def path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "decision_journal.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def append_action(data_dir: Path, event: dict) -> dict:
    row = normalize(event)
    with _lock, path(data_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def normalize(event: dict) -> dict:
    symbol = str(event.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol 不能为空")
    action = str(event.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        raise ValueError(f"未知决策动作: {action}")
    ts = int(event.get("ts") or time.time() * 1000)
    return {
        "ts": ts,
        "trade_date": event.get("trade_date") or _date_from_ts(ts).isoformat(),
        "symbol": symbol,
        "action": action,
        "status": STATUS_BY_ACTION.get(action),
        "side": str(event.get("side") or "").strip() or None,
        "price": _optional_float(event.get("price")),
        "note": str(event.get("note") or "").strip(),
        "source": "manual",
    }


def list_recent(
    data_dir: Path,
    *,
    target_date: date | None = None,
    symbol: str | None = None,
    days: int = 7,
) -> list[dict]:
    rows = _read_all(data_dir)
    if target_date is not None:
        ds = target_date.isoformat()
        rows = [r for r in rows if r.get("trade_date") == ds]
    elif days > 0:
        cutoff = int((time.time() - days * 86400) * 1000)
        rows = [r for r in rows if int(r.get("ts") or 0) >= cutoff]
    if symbol:
        s = symbol.upper()
        rows = [r for r in rows if str(r.get("symbol") or "").upper() == s]
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows


def latest_status_map(data_dir: Path, *, target_date: date | None = None) -> dict[str, dict]:
    rows = list_recent(data_dir, target_date=target_date or cn_today())
    latest: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("ts") or 0):
        if row.get("status"):
            latest[row["symbol"]] = row
    return latest


def timeline(data_dir: Path, symbol: str, *, target_date: date | None = None) -> list[dict]:
    return list_recent(data_dir, target_date=target_date or cn_today(), symbol=symbol)


def _read_all(data_dir: Path) -> list[dict]:
    p = path(data_dir)
    if not p.exists():
        return []
    out = []
    try:
        with _lock, p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        logger.warning("decision_journal read failed: %s", e)
    return out


def _date_from_ts(ts: int) -> date:
    try:
        return datetime.fromtimestamp(ts / 1000).date()
    except Exception:
        return cn_today()


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
