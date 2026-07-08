"""手动持仓记录。

本模块不连接券商账户, 只服务决策台的人工风控上下文。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "manual_positions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_all(data_dir: Path) -> list[dict]:
    p = path(data_dir)
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("manual_positions.json malformed: %s", e)
        return []
    rows = payload.get("positions") if isinstance(payload, dict) else payload
    out = []
    for row in rows or []:
        try:
            out.append(normalize(row))
        except ValueError:
            continue
    return out


def save_one(data_dir: Path, position: dict) -> dict:
    row = normalize(position)
    with _lock:
        rows = [r for r in load_all(data_dir) if r["symbol"] != row["symbol"]]
        rows.append(row)
        rows.sort(key=lambda r: r["symbol"])
        _write(data_dir, rows)
    return row


def delete_one(data_dir: Path, symbol: str) -> bool:
    target = _symbol(symbol)
    with _lock:
        rows = load_all(data_dir)
        kept = [r for r in rows if r["symbol"] != target]
        if len(kept) == len(rows):
            return False
        _write(data_dir, kept)
        return True


def import_many(data_dir: Path, positions: list[dict]) -> list[dict]:
    normalized = [normalize(row) for row in positions]
    by_symbol = {row["symbol"]: row for row in load_all(data_dir)}
    for row in normalized:
        by_symbol[row["symbol"]] = row
    rows = sorted(by_symbol.values(), key=lambda r: r["symbol"])
    with _lock:
        _write(data_dir, rows)
    return rows


def by_symbol(data_dir: Path) -> dict[str, dict]:
    return {row["symbol"]: row for row in load_all(data_dir)}


def normalize(row: dict) -> dict:
    symbol = _symbol(row.get("symbol"))
    now_ms = int(time.time() * 1000)
    shares = _num(row.get("shares"), default=0.0)
    cost_price = _num(row.get("cost_price"), default=0.0)
    if shares < 0:
        raise ValueError("shares 不能小于 0")
    if cost_price < 0:
        raise ValueError("cost_price 不能小于 0")
    out = {
        "symbol": symbol,
        "shares": shares,
        "cost_price": cost_price,
        "stop_loss_price": _optional_num(row.get("stop_loss_price")),
        "take_profit_price": _optional_num(row.get("take_profit_price")),
        "target_position_pct": _optional_num(row.get("target_position_pct")),
        "opened_at": row.get("opened_at") or now_ms,
        "updated_at": now_ms,
        "note": str(row.get("note") or "").strip(),
    }
    return out


def enrich(position: dict | None, latest_price: float | None) -> dict | None:
    if not position:
        return None
    pos = dict(position)
    price = latest_price if latest_price and latest_price > 0 else None
    shares = float(pos.get("shares") or 0)
    cost = float(pos.get("cost_price") or 0)
    stop = pos.get("stop_loss_price")
    target = pos.get("take_profit_price")
    pos["market_value"] = round(shares * price, 2) if price is not None else None
    pos["unrealized_pnl"] = round((price - cost) * shares, 2) if price is not None and cost else None
    pos["unrealized_pnl_pct"] = (price - cost) / cost if price is not None and cost else None
    pos["distance_to_stop_pct"] = (price - float(stop)) / price if price is not None and stop else None
    pos["distance_to_take_profit_pct"] = (float(target) - price) / price if price is not None and target else None
    pos["risk_amount"] = round(max(cost - float(stop), 0) * shares, 2) if stop and cost and shares else None
    pos["risk_level"], pos["position_action_hint"] = _risk_hint(pos, price)
    return pos


def _risk_hint(pos: dict, price: float | None) -> tuple[str, str]:
    if price is None:
        return "unknown", "暂无实时价格,请先确认行情源"
    stop = pos.get("stop_loss_price")
    target = pos.get("take_profit_price")
    if stop:
        stop = float(stop)
        if price <= stop:
            return "critical", "已跌破止损,请立即检查"
        if (price - stop) / price <= 0.01:
            return "warn", "接近止损,请确认是否手动处理"
    if target:
        target = float(target)
        if price >= target:
            return "take_profit", "已到达目标价,请确认是否止盈/减仓"
        if (target - price) / price <= 0.01:
            return "watch", "接近目标价,请确认是否止盈/减仓"
    return "normal", "持仓正常,无动作"


def _write(data_dir: Path, rows: list[dict]) -> None:
    path(data_dir).write_text(
        json.dumps({"positions": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _symbol(value) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("symbol 不能为空")
    return symbol


def _num(value, *, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _optional_num(value) -> float | None:
    if value is None or value == "":
        return None
    v = float(value)
    return v if v >= 0 else None
