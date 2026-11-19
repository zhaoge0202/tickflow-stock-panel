"""持仓守护 — 对策略已推送的买入信号做「买入后」的出场提醒。

背景: 策略监控只推「买入/卖出信号」, 而卖出信号依赖策略池状态; 后端重启后
策略池清空, 已经买入的票就失去自动提醒 (2026-11-19 实测: 重启后恒为科技
跌破 MA5 但没有任何推送)。本模块独立于策略池与监控规则, 只跟踪「最近 N 天
推送过的买入信号」, 按三条规则给出场提醒:

  1. 止损: 现价 <= 信号价 * (1 - 5%)
  2. 破位: 现价 < MA5 * 0.998 (入场当天不判, 避免自己刚买就报破位)
  3. 落袋: 现价 >= 信号价 * (1 + 2.5%) —— 日内浮盈提醒, 每天最多一次

状态落在 data/user_data/position_guard.json, 进程重启不丢; 首次运行会从
alerts.jsonl 回填最近 7 天的买入信号, 因此「加功能之前买的票」也能被守护。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STOP_PCT = -0.05          # 相对信号价
TAKE_PROFIT_PCT = 0.025   # 相对信号价, 日内落袋提醒
MA5_BREAK_TOL = 0.998     # 现价 < MA5*容差 视为破位
TRACK_DAYS = 10           # 自然日: 超过即停止跟踪 (约 7 个交易日)
BOOTSTRAP_DAYS = 2        # 首次运行回填的告警天数 (昨天+今天, 更早的已无出场意义)

_STRATEGY_LABELS = {
    "custom_dual_edge_focus": "双刃合-Focus",
    "custom_dual_edge_apex": "双刃合·Apex",
}

_LOCK = threading.RLock()
_STATE: dict | None = None      # {"version":1, "positions":[...]}
_STATE_DAY: str | None = None   # 跨日重置内存快照 (每日重新读盘)

# 当日已提醒账本: (symbol, 日期)。启动时从 alerts.jsonl 回灌, 保证「一只票一天只发一条」,
# 即使进程重启 / 状态文件被清空也不会重复推送。
_ALERTED_LOCK = threading.RLock()
_ALERTED_DAY: str | None = None
_ALERTED: set[tuple[str, str]] = set()


def _ensure_alerted_ledger(data_dir: Path, today_s: str) -> None:
    global _ALERTED_DAY, _ALERTED
    with _ALERTED_LOCK:
        if today_s == _ALERTED_DAY:
            return
        _ALERTED = set()
        try:
            from app.services import alert_store

            for ev in alert_store.list_recent(Path(data_dir), days=1, limit=500, source="guardian"):
                ts = int(ev.get("ts") or 0)
                sym = ev.get("symbol")
                if not sym or not ts:
                    continue
                if datetime.fromtimestamp(ts / 1000).date().isoformat() == today_s:
                    _ALERTED.add((str(sym), today_s))
        except Exception as exc:  # noqa: BLE001 — 账本读取失败最多重复一次
            logger.warning("持仓守护账本读取失败 (可能重复提醒一次): %s", exc)
        _ALERTED_DAY = today_s


def _path(data_dir: Path) -> Path:
    p = Path(data_dir) / "user_data" / "position_guard.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load(data_dir: Path) -> dict:
    global _STATE, _STATE_DAY
    today = date.today().isoformat()
    with _LOCK:
        if _STATE is not None and today == _STATE_DAY:
            return _STATE
        state = {"version": 1, "positions": []}
        p = _path(data_dir)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
                    state = raw
            except (OSError, ValueError) as exc:
                logger.warning("持仓守护状态读取失败, 按空状态继续: %s", exc)
        _STATE = state
        _STATE_DAY = today
        return state


def _save(data_dir: Path) -> None:
    with _LOCK:
        state = _STATE or {"version": 1, "positions": []}
        p = _path(data_dir)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)


def reset_state() -> None:
    """清空进程内状态与当日提醒账本 (测试用; 不删磁盘文件)。"""
    global _STATE, _STATE_DAY, _ALERTED_DAY, _ALERTED
    with _LOCK:
        _STATE = None
        _STATE_DAY = None
    with _ALERTED_LOCK:
        _ALERTED_DAY = None
        _ALERTED = set()


def list_positions(data_dir: Path) -> list[dict]:
    return [dict(item) for item in _load(data_dir)["positions"]]


def register(
    data_dir: Path,
    *,
    strategy_id: str,
    symbol: str,
    name: str | None,
    price: float | None,
    ts: int | None = None,
) -> bool:
    """登记一条买入信号; 同一 (策略, 标的, 信号日) 幂等, 价格以最新一次为准。"""
    try:
        entry = float(price or 0.0)
    except (TypeError, ValueError):
        return False
    if not strategy_id or not symbol or entry <= 0:
        return False
    ts = int(ts or time.time() * 1000)
    entry_day = datetime.fromtimestamp(ts / 1000).date().isoformat()
    with _LOCK:
        state = _load(data_dir)
        # 按标的主键去重: Focus 与 Apex 会同时推同一只票, 重复登记会导致双份提醒。
        for item in state["positions"]:
            if item.get("symbol") != symbol:
                continue
            # 同一标的多条信号: 只在新信号来自更晚的交易日时更新 (同日保留首次价格,
            # 也就是实际能买到的那个价; Focus/Apex 同时推同一只票不会互相覆盖)。
            if str(item.get("entry_date")) < entry_day:
                item["entry_price"] = entry
                item["name"] = name or item.get("name")
                item["strategy_id"] = strategy_id
                item["entry_date"] = entry_day
                item["entry_ts"] = ts
                _save(data_dir)
            return False
        state["positions"].append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "name": name or symbol,
            "entry_price": entry,
            "entry_ts": ts,
            "entry_date": entry_day,
            "status": "open",
            "stop_alerted": None,
            "ma5_alerted": None,
            "tp_alerted": None,
        })
        _save(data_dir)
        return True


def bootstrap_from_alerts(data_dir: Path) -> int:
    """首次运行 (进程内) 从 alerts.jsonl 回填最近 N 天的买入信号。"""
    try:
        from app.services import alert_store

        recent = alert_store.list_recent(
            Path(data_dir), days=BOOTSTRAP_DAYS, limit=2000, source="strategy",
        )
    except Exception as exc:  # noqa: BLE001 — 回填失败不影响新信号登记
        logger.warning("持仓守护回填失败: %s", exc)
        return 0
    added = 0
    # alert_store 按时间倒序返回; 按时间正序登记, 让「当天第一条信号」成为成本价
    # (实际能买到的价格), 后面的同日信号不会把成本抬高。
    for ev in sorted(recent, key=lambda e: e.get("ts") or 0):
        if ev.get("type") != "buy_signal":
            continue
        sid = ev.get("strategy_id")
        if not sid:
            continue
        added += int(register(
            data_dir,
            strategy_id=str(sid),
            symbol=str(ev.get("symbol") or ""),
            name=ev.get("name"),
            price=ev.get("price"),
            ts=ev.get("ts"),
        ))
    if added:
        logger.info("持仓守护: 从告警回填 %d 条买入信号", added)
    return added


def _label(strategy_id: str) -> str:
    return _STRATEGY_LABELS.get(strategy_id, strategy_id)


def _event(
    *, strategy_id: str, kind: str, row: dict, position: dict, price: float,
    ma5: float | None = None,
) -> dict:
    entry = float(position["entry_price"])
    pnl = price / entry - 1.0
    label = _label(strategy_id)
    name = row.get("name") or position.get("name") or position["symbol"]
    if kind == "stop":
        headline = f"触发 -5% 止损（现价 {price:.2f}，信号价 {entry:.2f}，{pnl * 100:+.1f}%）"
        severity = "warning"
        etype = "guardian_stop"
    elif kind == "ma5":
        headline = (
            f"跌破 MA5（现价 {price:.2f}，MA5 {ma5:.2f}，信号价 {entry:.2f}，{pnl * 100:+.1f}%）"
            if ma5 is not None else
            f"跌破 MA5（现价 {price:.2f}，信号价 {entry:.2f}，{pnl * 100:+.1f}%）"
        )
        severity = "warning"
        etype = "guardian_ma5"
    else:
        headline = f"日内浮盈 {pnl * 100:+.1f}%（现价 {price:.2f}，信号价 {entry:.2f}），建议落袋"
        severity = "info"
        etype = "guardian_tp"
    return {
        "ts": int(time.time() * 1000),
        "rule_id": f"guardian:{strategy_id}",
        "rule_name": f"持仓守护 · {label}",
        "strategy_id": strategy_id,
        "source": "guardian",
        "type": etype,
        "symbol": position["symbol"],
        "name": name,
        "message": f"{headline}",
        "price": round(price, 4),
        "change_pct": row.get("change_pct"),
        "signals": [],
        "severity": severity,
        "conditions": [],
        "logic": "and",
    }


def evaluate(data_dir: Path, frame, now: datetime | None = None) -> list[dict]:
    """对当前快照评估所有在守护的持仓, 返回需要推送的事件。

    frame: enriched 快照 (含 symbol / close / name / change_pct / ma5)。
    每种提醒对同一持仓每天最多一次; 触发止损后该持仓当日不再跟踪。
    """
    if frame is None or getattr(frame, "is_empty", lambda: True)():
        return []
    now = now or datetime.now()
    today = now.date()
    today_s = today.isoformat()
    _ensure_alerted_ledger(data_dir, today_s)
    with _LOCK:
        state = _load(data_dir)
        positions = [p for p in state["positions"] if p.get("status") == "open"]
        if not positions:
            return []
        try:
            rows = {str(r["symbol"]): r for r in frame.iter_rows(named=True)}
        except Exception:  # noqa: BLE001
            return []
        events: list[dict] = []
        changed = False
        keep: list[dict] = []
        for pos in positions:
            entry_day = pos.get("entry_date") or today_s
            try:
                age_days = (today - date.fromisoformat(entry_day)).days
            except ValueError:
                age_days = 0
            if age_days > TRACK_DAYS:
                changed = True
                continue
            row = rows.get(str(pos.get("symbol")))
            if not row:
                keep.append(pos)
                continue
            price = row.get("close")
            try:
                price = float(price)
            except (TypeError, ValueError):
                keep.append(pos)
                continue
            if not price or price <= 0:
                keep.append(pos)
                continue
            entry = float(pos["entry_price"])
            ma5 = row.get("ma5")
            try:
                ma5 = float(ma5) if ma5 is not None else None
            except (TypeError, ValueError):
                ma5 = None
            if ma5 is not None and not (ma5 > 0):
                ma5 = None

            # 一只票一天只发一条: 当天已提醒过 (含重启前的记录) 就不再发任何条件。
            already = (str(pos["symbol"]), today_s) in _ALERTED
            if price <= entry * (1.0 + STOP_PCT):
                pos["status"] = "stopped"
                changed = True
                if already or pos.get("stop_alerted") == today_s:
                    continue
                events.append(_event(strategy_id=pos["strategy_id"], kind="stop", row=row,
                                     position=pos, price=price))
                pos["stop_alerted"] = today_s
            elif (
                ma5 is not None
                and age_days >= 1
                and price < ma5 * MA5_BREAK_TOL
                and not already
                and pos.get("ma5_alerted") != today_s
            ):
                events.append(_event(strategy_id=pos["strategy_id"], kind="ma5", row=row,
                                     position=pos, price=price, ma5=ma5))
                pos["ma5_alerted"] = today_s
                changed = True
            elif (
                price >= entry * (1.0 + TAKE_PROFIT_PCT)
                and not already
                and pos.get("tp_alerted") != today_s
            ):
                events.append(_event(strategy_id=pos["strategy_id"], kind="tp", row=row,
                                     position=pos, price=price))
                pos["tp_alerted"] = today_s
                changed = True
            if pos.get("status") == "open" and pos.get("stop_alerted") != today_s:
                keep.append(pos)
        if changed:
            state["positions"] = keep
            _save(data_dir)
    with _ALERTED_LOCK:
        for ev in events:
            _ALERTED.add((str(ev.get("symbol")), today_s))
    return events
