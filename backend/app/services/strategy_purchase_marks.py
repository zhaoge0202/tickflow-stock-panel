"""策略页用户买入标记。

只记录用户在策略结果页主动确认的买入标记, 不代表券商真实成交或真实持仓。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def path(data_dir: Path) -> Path:
    target = data_dir / "user_data" / "strategy_purchase_marks.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def load_all(data_dir: Path) -> list[dict]:
    target = path(data_dir)
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("marks") if isinstance(payload, dict) else payload
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def save_one(data_dir: Path, mark: dict[str, Any]) -> dict:
    row = _normalize(mark)
    with _lock:
        rows = [item for item in load_all(data_dir) if not _same_key(item, row)]
        rows.append(row)
        rows.sort(key=lambda item: (
            str(item.get("signal_date") or ""),
            str(item.get("strategy_id") or ""),
            str(item.get("symbol") or ""),
        ), reverse=True)
        _write(data_dir, rows)
    return row


def delete_one(
    data_dir: Path,
    strategy_id: str,
    symbol: str,
    signal_date: str,
) -> bool:
    key = {
        "strategy_id": str(strategy_id).strip(),
        "symbol": str(symbol).strip().upper(),
        "signal_date": str(signal_date).strip(),
    }
    with _lock:
        rows = load_all(data_dir)
        kept = [item for item in rows if not _same_key(item, key)]
        if len(kept) == len(rows):
            return False
        _write(data_dir, kept)
    return True


def _normalize(mark: dict[str, Any]) -> dict:
    strategy_id = str(mark.get("strategy_id") or "").strip()
    symbol = str(mark.get("symbol") or "").strip().upper()
    signal_date = str(mark.get("signal_date") or "").strip()
    if not strategy_id:
        raise ValueError("strategy_id 不能为空")
    if not symbol:
        raise ValueError("symbol 不能为空")
    if not signal_date:
        raise ValueError("signal_date 不能为空")
    return {
        "strategy_id": strategy_id,
        "strategy_name": str(mark.get("strategy_name") or "").strip(),
        "symbol": symbol,
        "signal_date": signal_date,
        "signal_price": _optional_float(mark.get("signal_price")),
        "signal_score": _optional_float(mark.get("signal_score")),
        "signal_change_pct": _optional_float(mark.get("signal_change_pct")),
        "marked_at": int(mark.get("marked_at") or time.time() * 1000),
        "note": str(mark.get("note") or "").strip(),
    }


def _same_key(left: dict, right: dict) -> bool:
    return (
        str(left.get("strategy_id") or "").strip()
        == str(right.get("strategy_id") or "").strip()
        and str(left.get("symbol") or "").strip().upper()
        == str(right.get("symbol") or "").strip().upper()
        and str(left.get("signal_date") or "").strip()
        == str(right.get("signal_date") or "").strip()
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _write(data_dir: Path, rows: list[dict]) -> None:
    path(data_dir).write_text(
        json.dumps({"marks": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
