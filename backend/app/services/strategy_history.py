"""策略候选生命周期历史。

策略缓存只保留当前快照，告警记录也有较短保留期。本模块保存策略选出、
竞价确认/淘汰及买卖信号，供策略复盘使用。文件采用 JSONL 追加写，
通过 event_key 去重，避免盘中轮询把同一节点重复落盘。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")

MAX_DAYS = 180
MAX_RECORDS = 50_000
PRUNE_EVERY = 500
_lock = threading.Lock()
_write_count = 0
_known_keys: dict[str, set[str]] = {}


def path(data_dir: Path) -> Path:
    target = data_dir / "user_data" / "strategy_history.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def append_many(data_dir: Path, events: list[dict[str, Any]]) -> int:
    """追加新的历史节点，返回实际写入数量。"""
    if not events:
        return 0
    target = path(data_dir)
    key_id = str(target)
    with _lock:
        known = _load_known_keys_locked(target, key_id)
        fresh: list[dict] = []
        for event in events:
            row = _normalize(event)
            event_key = row["event_key"]
            if event_key in known:
                continue
            known.add(event_key)
            fresh.append(row)
        if not fresh:
            return 0
        with target.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        global _write_count
        _write_count += len(fresh)
        if _write_count >= PRUNE_EVERY:
            _write_count = 0
            _prune_locked(target, known)
        return len(fresh)


def record_selection_snapshot(
    data_dir: Path,
    *,
    strategy_id: str,
    strategy_name: str,
    signal_date: str,
    rows: list[dict],
    trade_date: str | None = None,
    mode: str = "post_close",
) -> int:
    """记录一次策略结果中的候选股票。"""
    target_trade_date = trade_date or _next_weekday(signal_date)
    events = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        event_type = "preselect" if mode == "preselect" else "selected"
        events.append({
            "event_key": (
                f"{event_type}:{strategy_id}:{symbol}:{signal_date}:"
                f"{target_trade_date}:{mode}"
            ),
            "event_type": event_type,
            "status": "watch_only" if event_type == "preselect" else "selected",
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "symbol": symbol,
            "name": row.get("name"),
            "signal_date": signal_date,
            "trade_date": target_trade_date,
            "phase": mode,
            "price": row.get("close"),
            "change_pct": row.get("change_pct"),
            "score": row.get("score"),
            "signals": row.get("signals") or [],
            "reason_code": None,
            "reason": (
                "盘后预选，仅供观察，等待下一交易日竞价确认"
                if event_type == "preselect"
                else "盘后策略选出，等待下一交易日竞价确认"
            ),
            "metadata": {},
        })
    return append_many(data_dir, events)


def record_monitor_events(data_dir: Path, events: list[dict]) -> int:
    """把策略监控的买卖节点同步到长期历史。"""
    rows = []
    for event in events:
        if event.get("source") != "strategy":
            continue
        event_type = str(event.get("type") or "")
        if event_type not in {"buy_signal", "sell_signal", "pool_entry", "pool_exit"}:
            continue
        strategy_id = str(event.get("strategy_id") or "").strip()
        symbol = str(event.get("symbol") or "").strip().upper()
        if not strategy_id or not symbol or symbol == "_BATCH":
            continue
        signal_date = _date_from_ts(event.get("ts"))
        rows.append({
            "event_key": f"monitor:{strategy_id}:{symbol}:{event_type}:{signal_date}",
            "event_type": event_type,
            "status": "signal",
            "strategy_id": strategy_id,
            "strategy_name": event.get("rule_name") or strategy_id,
            "symbol": symbol,
            "name": event.get("name"),
            "signal_date": signal_date,
            "trade_date": signal_date,
            "phase": "intraday",
            "price": event.get("price"),
            "change_pct": event.get("change_pct"),
            "score": None,
            "signals": event.get("signals") or [],
            "reason_code": event_type,
            "reason": event.get("message") or event_type,
            "metadata": {"rule_id": event.get("rule_id")},
        })
    return append_many(data_dir, rows)


def backfill_monitor_events(
    data_dir: Path,
    *,
    strategy_id: str | None = None,
    days: int = MAX_DAYS,
) -> int:
    """把已有告警记录迁移到策略生命周期历史，按 event_key 自动去重。"""
    from app.services import alert_store

    events = alert_store.list_recent(
        data_dir,
        days=max(1, min(int(days or MAX_DAYS), MAX_DAYS)),
        limit=MAX_RECORDS,
        source="strategy",
    )
    if strategy_id:
        events = [
            event for event in events
            if event.get("strategy_id") == strategy_id
        ]
    return record_monitor_events(data_dir, events)


def record_auction_outcomes(data_dir: Path, outcomes: list[dict[str, Any]]) -> int:
    """记录竞价确认、未确认节点。调用方负责提供 reason。"""
    return append_many(data_dir, outcomes)


def list_events(
    data_dir: Path,
    *,
    strategy_id: str | None = None,
    symbol: str | None = None,
    signal_date: str | None = None,
    trade_date: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    days: int = MAX_DAYS,
    limit: int = 1000,
) -> list[dict]:
    cutoff = int((time.time() - max(1, min(days, MAX_DAYS)) * 86400) * 1000)
    target_symbol = str(symbol or "").strip().upper() or None
    rows: list[dict] = []
    target = path(data_dir)
    if not target.exists():
        return rows
    try:
        with _lock, target.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(row.get("ts") or 0) < cutoff:
                    continue
                if strategy_id and row.get("strategy_id") != strategy_id:
                    continue
                if target_symbol and str(row.get("symbol") or "").upper() != target_symbol:
                    continue
                if signal_date and row.get("signal_date") != signal_date:
                    continue
                if trade_date and row.get("trade_date") != trade_date:
                    continue
                if event_type and row.get("event_type") != event_type:
                    continue
                if status and row.get("status") != status:
                    continue
                rows.append(row)
    except OSError as exc:
        logger.warning("读取策略历史失败: %s", exc)
        return []
    rows.sort(
        key=lambda row: (
            int(row.get("ts") or 0),
            _event_sort_rank(str(row.get("event_type") or "")),
        ),
        reverse=True,
    )
    return rows[: max(1, min(int(limit or 1000), MAX_RECORDS))]


def _normalize(event: dict[str, Any]) -> dict:
    event_type = str(event.get("event_type") or "").strip()
    if not event_type:
        raise ValueError("event_type 不能为空")
    strategy_id = str(event.get("strategy_id") or "").strip()
    symbol = str(event.get("symbol") or "").strip().upper()
    if not strategy_id or not symbol:
        raise ValueError("strategy_id 和 symbol 不能为空")
    ts = int(event.get("ts") or time.time() * 1000)
    event_key = str(event.get("event_key") or "").strip()
    if not event_key:
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        event_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "ts": ts,
        "event_key": event_key,
        "event_type": event_type,
        "status": str(event.get("status") or "").strip() or None,
        "strategy_id": strategy_id,
        "strategy_name": str(event.get("strategy_name") or "").strip(),
        "symbol": symbol,
        "name": str(event.get("name") or "").strip(),
        "signal_date": str(event.get("signal_date") or _date_from_ts(ts)),
        "trade_date": str(event.get("trade_date") or "") or None,
        "phase": str(event.get("phase") or "").strip() or None,
        "price": _optional_float(event.get("price")),
        "change_pct": _optional_float(event.get("change_pct")),
        "score": _optional_float(event.get("score")),
        "signals": [str(item) for item in event.get("signals") or [] if item],
        "reason_code": str(event.get("reason_code") or "").strip() or None,
        "reason": str(event.get("reason") or "").strip(),
        "metadata": event.get("metadata") if isinstance(event.get("metadata"), dict) else {},
    }


def _load_known_keys_locked(target: Path, key_id: str) -> set[str]:
    known = _known_keys.get(key_id)
    if known is not None:
        return known
    known = set()
    if target.exists():
        try:
            with target.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = str(row.get("event_key") or "").strip()
                    if key:
                        known.add(key)
        except OSError:
            pass
    _known_keys[key_id] = known
    return known


def _prune_locked(target: Path, known: set[str]) -> None:
    cutoff = int((time.time() - MAX_DAYS * 86400) * 1000)
    kept: list[dict] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(row.get("ts") or 0) >= cutoff:
                    kept.append(row)
    except OSError:
        return
    if len(kept) > MAX_RECORDS:
        kept.sort(key=lambda row: int(row.get("ts") or 0))
        kept = kept[-MAX_RECORDS:]
    try:
        tmp = target.with_name(target.name + ".tmp")
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in kept)
        tmp.write_text(text + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(target)
        known.clear()
        known.update(str(row.get("event_key")) for row in kept if row.get("event_key"))
    except OSError as exc:
        logger.warning("清理策略历史失败: %s", exc)


def _date_from_ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=CN_TZ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return date.today().isoformat()


def _next_weekday(value: str) -> str:
    try:
        day = date.fromisoformat(str(value)[:10]) + timedelta(days=1)
    except ValueError:
        day = date.today() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_sort_rank(event_type: str) -> int:
    """同毫秒写入时，让后置的确认结果排在候选快照之前。"""
    return {
        "auction_confirmed": 4,
        "auction_rejected": 4,
        "buy_signal": 3,
        "sell_signal": 3,
        "pool_entry": 2,
        "pool_exit": 2,
        "selected": 1,
        "preselect": 0,
    }.get(event_type, 0)
