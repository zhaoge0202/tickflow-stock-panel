"""盘中决策队列聚合。

DecisionItem 是 alerts、SignalFrame、手动持仓和人工处理日志的视图层。
它不会产生交易委托, 只帮助用户决定下一步去券商软件手动处理。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.market_time import cn_today
from app.services import (
    alert_store,
    decision_journal,
    manual_positions,
    quote_tick_store,
    signal_frame,
)
from app.services.symbols import normalize_symbol

ACTIVE_STATUSES = {"pending", "waiting", "planned"}
DONE_STATUSES = {"manual_done", "ignored"}


def build_queue(
    data_dir: Path,
    repo,
    *,
    target_date: date | None = None,
    status: str | None = None,
) -> dict:
    target_date = target_date or cn_today()
    alerts = _alerts_for_date(data_dir, target_date, repo)
    positions = manual_positions.by_symbol(data_dir, repo)
    latest_status = _latest_status_map(data_dir, repo, target_date)
    symbols = _symbols_from_inputs(alerts, positions, latest_status)
    frames = {
        f["symbol"]: f
        for f in signal_frame.build_latest_frames(
            data_dir,
            repo,
            symbols=sorted(symbols),
            target_date=target_date,
            include_levels=True,
        )
    }
    items = [
        _build_item(
            target_date=target_date,
            symbol=symbol,
            alerts=[a for a in alerts if a.get("symbol") == symbol],
            frame=frames.get(symbol),
            position=positions.get(symbol),
            journal=latest_status.get(symbol),
        )
        for symbol in sorted(symbols)
    ]
    items = [item for item in items if item is not None]
    if status:
        items = [item for item in items if item["status"] == status]
    items.sort(key=lambda item: (item["priority"], item.get("last_event_ts") or 0), reverse=True)
    quality = quote_tick_store.quality(data_dir, sorted(symbols), target_date=target_date)
    return {
        "trade_date": target_date.isoformat(),
        "items": items,
        "total": len(items),
        "pending": sum(1 for item in items if item["status"] in ACTIVE_STATUSES),
        "done": sum(1 for item in items if item["status"] in DONE_STATUSES),
        "quality": quality,
    }


def summary(data_dir: Path, repo=None, *, target_date: date | None = None) -> dict:
    """轻量状态条摘要。

    完整队列会构建 signal frame, 盘中或数据量大时可能较慢; 状态条只需要数量和
    quote quality, 应避免被完整队列拖住。
    """
    target_date = target_date or cn_today()
    alerts = _alerts_for_date(data_dir, target_date, repo)
    positions = manual_positions.by_symbol(data_dir, repo)
    latest_status = _latest_status_map(data_dir, repo, target_date)
    symbols = _symbols_from_inputs(alerts, positions, latest_status)
    statuses = {
        symbol: (latest_status.get(symbol) or {}).get("status") or "pending"
        for symbol in symbols
    }
    pending = sum(1 for status in statuses.values() if status in ACTIVE_STATUSES)
    done = sum(1 for status in statuses.values() if status in DONE_STATUSES)
    quality = quote_tick_store.quality(data_dir, sorted(symbols), target_date=target_date)
    if not symbols:
        quality = {
            **quality,
            "missing_symbols": [],
            "stale_symbols": [],
        }
    return {
        "trade_date": target_date.isoformat(),
        "total": len(symbols),
        "pending": pending,
        "done": done,
        "quality": quality,
    }


def get_item(data_dir: Path, repo, symbol: str, *, target_date: date | None = None) -> dict | None:
    target_date = target_date or cn_today()
    target = _normalize_symbol(symbol, repo)
    alerts = [
        alert
        for alert in _alerts_for_date(data_dir, target_date, repo)
        if alert.get("symbol") == target
    ]
    detail = signal_frame.build_detail(data_dir, repo, target, target_date=target_date)
    positions = manual_positions.by_symbol(data_dir, repo)
    journal = _latest_status_map(data_dir, repo, target_date).get(target)
    item = _build_item(
        target_date=target_date,
        symbol=target,
        alerts=alerts,
        frame=detail,
        position=positions.get(target),
        journal=journal,
    )
    if item:
        item["signal_frame"] = detail or item.get("signal_frame")
        item["timeline"] = timeline(data_dir, target, repo, target_date=target_date)
    return item


def record_action(data_dir: Path, repo, symbol: str, payload: dict) -> dict:
    event = dict(payload)
    event["symbol"] = _normalize_symbol(symbol, repo)
    row = decision_journal.append_action(data_dir, event)
    return {"ok": True, "event": row}


def timeline(data_dir: Path, symbol: str, repo=None, *, target_date: date | None = None) -> list[dict]:
    target_date = target_date or cn_today()
    target = _normalize_symbol(symbol, repo)
    alerts = [
        {
            "kind": "alert",
            "ts": ev.get("ts"),
            "symbol": target,
            "title": ev.get("rule_name") or ev.get("message") or ev.get("type"),
            "message": ev.get("message"),
            "source": ev.get("source"),
            "type": ev.get("type"),
            "price": ev.get("price"),
            "severity": ev.get("severity"),
            "signals": ev.get("signals") or [],
        }
        for ev in _alerts_for_date(data_dir, target_date, repo)
        if ev.get("symbol") == target
    ]
    journals = [
        {
            "kind": "journal",
            "ts": ev.get("ts"),
            "symbol": target,
            "title": _action_label(ev.get("action")),
            "message": ev.get("note") or _action_label(ev.get("action")),
            "action": ev.get("action"),
            "status": ev.get("status"),
            "side": ev.get("side"),
            "price": ev.get("price"),
        }
        for ev in _journal_timeline(data_dir, target, repo, target_date)
    ]
    out = alerts + journals
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out


def _build_item(
    *,
    target_date: date,
    symbol: str,
    alerts: list[dict],
    frame: dict | None,
    position: dict | None,
    journal: dict | None,
) -> dict | None:
    if not alerts and not frame and not position:
        return None
    status = journal.get("status") if journal and journal.get("status") else "pending"
    pos = frame.get("position") if frame and frame.get("position") else manual_positions.enrich(position, _price(frame))
    signals = _unique([
        *[s for ev in alerts for s in (ev.get("signals") or [])],
        *((frame or {}).get("active_signals") or []),
    ])
    risk_flags = list((frame or {}).get("risk_flags") or [])
    source_tags = _unique([
        *[str(ev.get("source") or ev.get("type") or "alert") for ev in alerts],
        *(["position"] if pos else []),
        *(["signal_frame"] if frame else []),
    ])
    reasons = _unique([
        *[str(ev.get("message") or ev.get("rule_name") or ev.get("type")) for ev in alerts if ev.get("message") or ev.get("rule_name")],
        *([frame.get("reason_text")] if frame and frame.get("reason_text") else []),
        *([pos.get("position_action_hint")] if pos and pos.get("position_action_hint") else []),
    ])
    side = _side(alerts, signals, risk_flags, pos)
    priority = _priority(status, alerts, frame, pos, side)
    latest_ts = max([int(ev.get("ts") or 0) for ev in alerts] + [int((frame or {}).get("ts") or 0), int((journal or {}).get("ts") or 0)])
    return {
        "id": f"{target_date.isoformat()}:{symbol}",
        "trade_date": target_date.isoformat(),
        "symbol": symbol,
        "name": (frame or {}).get("name") or (alerts[0].get("name") if alerts else None),
        "side": side,
        "priority": priority,
        "status": status,
        "source_tags": source_tags,
        "latest_price": _price(frame) or (alerts[0].get("price") if alerts else None),
        "change_pct": (frame or {}).get("change_pct") or (alerts[0].get("change_pct") if alerts else None),
        "amount": (frame or {}).get("amount"),
        "quote_freshness": (frame or {}).get("quote_freshness") or "unknown",
        "reasons": reasons,
        "signals": signals,
        "risk_flags": risk_flags,
        "position": pos,
        "risk": _risk_summary(pos, risk_flags),
        "last_event_ts": latest_ts or None,
        "signal_frame": frame,
        "alert_count": len(alerts),
    }


def _alerts_for_date(data_dir: Path, target_date: date, repo=None) -> list[dict]:
    # alerts_store 只能按最近 days 查, 这里再按交易日精确过滤。
    events = alert_store.list_recent(data_dir, days=30, limit=5000)
    ds = target_date.isoformat()
    out = []
    for ev in events:
        symbol = _normalize_symbol(ev.get("symbol"), repo)
        if not symbol:
            continue
        ts = int(ev.get("ts") or 0)
        if ts and _date_from_ms(ts) != ds:
            continue
        row = dict(ev)
        row["symbol"] = symbol
        out.append(row)
    return out


def _latest_status_map(data_dir: Path, repo, target_date: date) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in decision_journal.latest_status_map(data_dir, target_date=target_date).values():
        symbol = _normalize_symbol(row.get("symbol"), repo)
        if not symbol:
            continue
        normalized = dict(row)
        normalized["symbol"] = symbol
        prev = out.get(symbol)
        if prev is None or int(normalized.get("ts") or 0) >= int(prev.get("ts") or 0):
            out[symbol] = normalized
    return out


def _journal_timeline(data_dir: Path, target: str, repo, target_date: date) -> list[dict]:
    out = []
    for row in decision_journal.list_recent(data_dir, target_date=target_date):
        symbol = _normalize_symbol(row.get("symbol"), repo)
        if symbol != target:
            continue
        normalized = dict(row)
        normalized["symbol"] = symbol
        out.append(normalized)
    return out


def _normalize_symbol(value, repo=None) -> str:
    return normalize_symbol(str(value or ""), repo)


def _symbols_from_inputs(alerts: list[dict], positions: dict[str, dict], journals: dict[str, dict]) -> set[str]:
    symbols = {str(ev.get("symbol") or "").upper() for ev in alerts if ev.get("symbol")}
    symbols.update(positions.keys())
    symbols.update(journals.keys())
    return {s for s in symbols if s}


def _side(alerts: list[dict], signals: list[str], risk_flags: list[str], position: dict | None) -> str:
    if position and any(flag in risk_flags for flag in ("stop_loss_break", "stop_loss_near")):
        return "sell_risk"
    if any(ev.get("type") == "dropped" for ev in alerts):
        return "sell_risk"
    if risk_flags:
        return "risk"
    if any(ev.get("type") == "new_entry" or ev.get("source") == "strategy" for ev in alerts):
        return "buy_watch"
    if any(sig in {"open_range_breakout", "vwap_breakout", "pullback_near_support"} for sig in signals):
        return "buy_watch"
    return "watch"


def _priority(status: str, alerts: list[dict], frame: dict | None, position: dict | None, side: str) -> int:
    score = int((frame or {}).get("decision_score") or 0)
    if side == "sell_risk":
        score += 100
    elif side == "risk":
        score += 60
    elif side == "buy_watch":
        score += 50
    if any(ev.get("severity") == "critical" for ev in alerts):
        score += 80
    if any(ev.get("severity") == "warn" for ev in alerts):
        score += 30
    if position:
        score += 20
    if status == "manual_done":
        score -= 100
    if status == "ignored":
        score -= 200
    if status == "planned":
        score += 10
    return score


def _risk_summary(position: dict | None, risk_flags: list[str]) -> dict | None:
    if not position and not risk_flags:
        return None
    return {
        "risk_flags": risk_flags,
        "risk_level": position.get("risk_level") if position else ("warn" if risk_flags else "normal"),
        "hint": position.get("position_action_hint") if position else None,
        "risk_amount": position.get("risk_amount") if position else None,
        "distance_to_stop_pct": position.get("distance_to_stop_pct") if position else None,
    }


def _date_from_ms(ts: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts / 1000).date().isoformat()


def _price(frame: dict | None) -> float | None:
    if not frame:
        return None
    return frame.get("latest_price") or frame.get("price")


def _unique(items: list[str]) -> list[str]:
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _action_label(action: str | None) -> str:
    return {
        "mark_wait": "继续等",
        "mark_plan": "准备手动下单",
        "mark_manual_done": "已手动处理",
        "mark_ignore": "忽略今日",
        "note": "备注",
        "position_update": "更新手动持仓",
    }.get(str(action or ""), "人工记录")
