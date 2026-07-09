"""盘中提醒回放。

聚焦验证提醒系统, 不模拟下单, 不写入真实 alerts.jsonl。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl

from app.services import quote_tick_store, signal_frame
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

logger = logging.getLogger(__name__)

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="intraday-replay")
CN_TZ = ZoneInfo("Asia/Shanghai")
REPLAY_MIN_EVENT_INTERVAL_MS = 60_000
DEFAULT_REPLAY_RULES = [
    ("replay_open_range_breakout", "开盘区间突破", "signal_open_range_breakout", "buy_watch"),
    ("replay_vwap_breakout", "上穿 VWAP", "signal_vwap_breakout", "buy_watch"),
    ("replay_volume_surge_1m", "1分钟放量", "signal_volume_surge_1m", "watch"),
    ("replay_volume_surge_5m", "5分钟放量", "signal_volume_surge_5m", "watch"),
    ("replay_near_resistance", "接近压力", "signal_near_resistance", "risk"),
    ("replay_stop_loss_break", "跌破手动止损", "signal_stop_loss_break", "sell_risk"),
]


def run_replay(
    data_dir: Path,
    *,
    target_date: date,
    symbols: list[str],
    start_time: str | None = None,
    end_time: str | None = None,
    task_id: str | None = None,
) -> dict:
    task_id = task_id or uuid4().hex
    requested_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    normalized_symbols = _normalize_symbols(requested_symbols)
    loaded = _load_replay_ticks(
        data_dir,
        target_date=target_date,
        symbols=normalized_symbols,
        start_time=start_time,
        end_time=end_time,
    )
    ticks = loaded["ticks"]
    rules = _replay_rules(data_dir)
    events = _simulate(
        data_dir,
        ticks,
        target_date=target_date,
        start_time=start_time,
        end_time=end_time,
        rules=rules,
    )
    events = [_attach_outcome(ev, ticks) for ev in events]
    result = {
        "task_id": task_id,
        "status": "succeeded",
        "date": target_date.isoformat(),
        "requested_symbols": requested_symbols,
        "symbols": normalized_symbols,
        "tick_source": loaded["tick_source"],
        "tick_count": len(ticks),
        "window_tick_count": loaded["window_tick_count"],
        "quote_tick_count": loaded["quote_tick_count"],
        "quote_window_tick_count": loaded["quote_window_tick_count"],
        "trade_tick_count": loaded["trade_tick_count"],
        "trade_window_tick_count": loaded["trade_window_tick_count"],
        "tick_time_range": _tick_time_range(ticks),
        "window_time_range": _tick_time_range(_window_ticks(ticks, start_time=start_time, end_time=end_time)),
        "fallback_error": loaded.get("fallback_error"),
        "rule_count": len(rules),
        "triggered": len(events),
        "events": events,
        "summary": _summary(events, key="signal"),
        "rule_summary": _summary(events, key="rule_id"),
        "finished_at": int(time.time() * 1000),
    }
    _set_task(task_id, result)
    return result


def enqueue_replay(
    data_dir: Path,
    *,
    target_date: date,
    symbols: list[str],
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict:
    task_id = uuid4().hex
    requested_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    task = {
        "task_id": task_id,
        "status": "running",
        "date": target_date.isoformat(),
        "requested_symbols": requested_symbols,
        "symbols": _normalize_symbols(requested_symbols),
        "start_time": start_time,
        "end_time": end_time,
        "started_at": int(time.time() * 1000),
    }
    _set_task(task_id, task)
    _EXECUTOR.submit(
        _run_replay_job,
        task_id,
        data_dir,
        target_date=target_date,
        symbols=symbols,
        start_time=start_time,
        end_time=end_time,
    )
    return task


def get_task(task_id: str) -> dict | None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def _set_task(task_id: str, task: dict) -> None:
    with _TASKS_LOCK:
        _TASKS[task_id] = dict(task)


def _run_replay_job(
    task_id: str,
    data_dir: Path,
    *,
    target_date: date,
    symbols: list[str],
    start_time: str | None,
    end_time: str | None,
) -> None:
    try:
        run_replay(
            data_dir,
            target_date=target_date,
            symbols=symbols,
            start_time=start_time,
            end_time=end_time,
            task_id=task_id,
        )
    except Exception as e:
        logger.exception("盘中回放任务失败(task_id=%s)", task_id)
        current = get_task(task_id) or {"task_id": task_id}
        current.update({
            "status": "failed",
            "error": str(e),
            "finished_at": int(time.time() * 1000),
        })
        _set_task(task_id, current)


def _load_replay_ticks(
    data_dir: Path,
    *,
    target_date: date,
    symbols: list[str],
    start_time: str | None,
    end_time: str | None,
) -> dict:
    """装载回放 tick。

    quote_ticks 是实时快照事实层; 如果服务没有在盘中运行, 某个标的往往只会
    留下收盘后的快照。此时回放临时拉取 tdxapi 逐笔成交并转换为累计序列,
    只用于本次模拟, 不写回 quote_ticks。
    """
    quote_ticks = quote_tick_store.read_ticks(
        data_dir,
        target_date=target_date,
        symbols=symbols,
        prefer_hot=target_date == datetime.now(CN_TZ).date(),
    )
    quote_window_count = _count_window_ticks(quote_ticks, start_time=start_time, end_time=end_time)
    loaded = {
        "ticks": quote_ticks,
        "tick_source": "quote_ticks",
        "window_tick_count": quote_window_count,
        "quote_tick_count": len(quote_ticks),
        "quote_window_tick_count": quote_window_count,
        "trade_tick_count": 0,
        "trade_window_tick_count": 0,
        "fallback_error": None,
    }
    if quote_window_count > 0:
        return loaded

    try:
        trade_loaded = _load_trade_tick_replay_ticks(
            symbols,
            target_date,
            start_time=start_time,
            end_time=end_time,
        )
    except Exception as e:
        loaded["fallback_error"] = str(e)
        return loaded

    trade_ticks = trade_loaded["ticks"]
    trade_window_count = _count_window_ticks(trade_ticks, start_time=start_time, end_time=end_time)
    loaded["trade_tick_count"] = len(trade_ticks)
    loaded["trade_window_tick_count"] = trade_window_count
    loaded["fallback_error"] = trade_loaded.get("fallback_error")
    if trade_window_count > 0 or (not quote_ticks and trade_ticks):
        loaded["ticks"] = trade_ticks
        loaded["tick_source"] = trade_loaded["tick_source"]
        loaded["window_tick_count"] = trade_window_count
    return loaded


def _load_trade_tick_replay_ticks(
    symbols: list[str],
    target_date: date,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict:
    from app.plugins.tdxapi.provider import TDXAPIProvider

    provider = TDXAPIProvider()
    try:
        out: list[dict] = []
        for symbol in symbols:
            rows = provider.get_trade_ticks(symbol, target_date, mode="all", limit=None)
            out.extend(_trade_ticks_to_quote_rows(symbol, rows, target_date, source="tdxapi_trade_ticks"))
        out.sort(key=lambda r: (int(r.get("event_ts") or 0), str(r.get("symbol") or "")))
        if _count_window_ticks(out, start_time=start_time, end_time=end_time) > 0:
            return {"ticks": out, "tick_source": "tdxapi_trade_ticks", "fallback_error": None}

        fallback_error = None
        history_out: list[dict] = []
        try:
            for symbol in symbols:
                rows = provider.get_trade_history_full(
                    symbol,
                    start_date=target_date,
                    end_date=target_date,
                    include_today=target_date == datetime.now(CN_TZ).date(),
                    limit=None,
                )
                history_out.extend(_trade_ticks_to_quote_rows(
                    symbol,
                    rows,
                    target_date,
                    source="tdxapi_trade_history_minute_precision",
                ))
        except Exception as e:
            fallback_error = str(e)

        if history_out:
            history_out.sort(key=lambda r: (int(r.get("event_ts") or 0), str(r.get("symbol") or "")))
            return {
                "ticks": history_out,
                "tick_source": "tdxapi_trade_history_minute_precision",
                "fallback_error": fallback_error,
            }
        return {"ticks": out, "tick_source": "tdxapi_trade_ticks", "fallback_error": fallback_error}
    finally:
        provider.close()


def _trade_ticks_to_quote_rows(
    symbol: str,
    rows: list[dict],
    target_date: date,
    *,
    source: str,
) -> list[dict]:
    parsed = []
    for row in rows or []:
        dt = _trade_tick_datetime(row.get("datetime"))
        price = _num(row.get("price"))
        if dt is None or dt.date() != target_date or price is None:
            continue
        parsed.append({
            "row": row,
            "base_ts": int(dt.timestamp() * 1000),
            "seq": int(row.get("seq_in_day") or 0),
            "price": price,
        })
    parsed.sort(key=lambda item: (item["base_ts"], item["seq"]))
    minute_counts = Counter(item["base_ts"] for item in parsed)
    minute_seen: dict[int, int] = defaultdict(int)

    out: list[dict] = []
    open_price = None
    high_price = None
    low_price = None
    cumulative_volume = 0.0
    cumulative_amount = 0.0
    ingest_ts = int(time.time() * 1000)
    for item in parsed:
        row = item["row"]
        base_ts = int(item["base_ts"])
        seen = minute_seen[base_ts]
        minute_seen[base_ts] += 1
        total = max(1, minute_counts[base_ts])
        offset_ms = min(59_999, int(seen * 60_000 / total)) if total > 1 else 0
        event_ts = base_ts + offset_ms
        event_dt = datetime.fromtimestamp(event_ts / 1000, tz=CN_TZ)
        price = float(item["price"])
        volume = _num(row.get("volume")) or 0.0
        amount = _num(row.get("amount"))
        if amount is None:
            amount = price * volume * 100.0
        cumulative_volume += volume
        cumulative_amount += amount
        open_price = price if open_price is None else open_price
        high_price = price if high_price is None else max(high_price, price)
        low_price = price if low_price is None else min(low_price, price)
        raw = {
            "seq_in_day": row.get("seq_in_day"),
            "side": row.get("side"),
            "side_label": row.get("side_label"),
            "order_count": row.get("order_count"),
            "source": row.get("source") or source,
            "time_precision": "minute",
        }
        out.append({
            "symbol": symbol,
            "name": row.get("name"),
            "source": source,
            "event_ts": event_ts,
            "ingest_ts": ingest_ts,
            "trade_date": target_date.isoformat(),
            "hour": f"{event_dt.hour:02d}",
            "last_price": price,
            "prev_close": None,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "volume": cumulative_volume,
            "amount": cumulative_amount,
            "bid1": None,
            "ask1": None,
            "bid1_vol": None,
            "ask1_vol": None,
            "raw": json.dumps(raw, ensure_ascii=False, default=str),
        })
    return out


def _trade_tick_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ)


def _simulate(
    data_dir: Path,
    ticks: list[dict],
    *,
    target_date: date,
    start_time: str | None,
    end_time: str | None,
    rules: list[dict],
) -> list[dict]:
    engine = MonitorRuleEngine()
    engine.set_rules(rules)
    by_symbol: dict[str, list[dict]] = {}
    latest_by_symbol: dict[str, dict] = {}
    last_signal_by_symbol: dict[tuple[str, str], int] = {}
    cooldown_by_rule = {
        str(rule.get("id") or ""): _rule_replay_cooldown_ms(rule)
        for rule in rules
    }
    events = []
    for row in ticks:
        ts = int(row.get("event_ts") or row.get("ingest_ts") or 0)
        if not _in_time_window(ts, start_time, end_time):
            continue
        symbol = row.get("symbol")
        if not symbol or _num(row.get("last_price")) is None:
            continue
        by_symbol.setdefault(symbol, []).append(row)
        latest_by_symbol[symbol] = row
        frames = signal_frame.build_frames_from_tick_rows(
            data_dir,
            None,
            ticks_by_symbol={symbol: by_symbol[symbol]},
            latest_by_symbol={symbol: row},
            symbols=[symbol],
            target_date=target_date,
            include_levels=False,
            include_trade_summary=False,
        )
        if not frames:
            continue
        frame = frames[0]
        df = pl.DataFrame([_frame_to_monitor_row(frame)])
        for ev in engine.evaluate(df):
            signal = _event_signal(ev)
            dedupe_key = (str(ev.get("rule_id") or ""), symbol)
            cooldown_ms = cooldown_by_rule.get(dedupe_key[0], REPLAY_MIN_EVENT_INTERVAL_MS)
            last_ts = last_signal_by_symbol.get(dedupe_key)
            if last_ts is not None and ts - last_ts < cooldown_ms:
                continue
            last_signal_by_symbol[dedupe_key] = ts
            events.append({
                "ts": ts,
                "symbol": symbol,
                "name": frame.get("name"),
                "price": frame.get("latest_price"),
                "rule_id": ev.get("rule_id"),
                "rule_name": ev.get("rule_name"),
                "source": ev.get("source"),
                "type": ev.get("type"),
                "signal": signal,
                "message": ev.get("message") or frame.get("reason_text"),
                "reason_text": frame.get("reason_text"),
                "signals": frame.get("active_signals") or [],
                "risk_flags": frame.get("risk_flags") or [],
            })
    return events


def _count_window_ticks(ticks: list[dict], *, start_time: str | None, end_time: str | None) -> int:
    return len(_window_ticks(ticks, start_time=start_time, end_time=end_time))


def _window_ticks(ticks: list[dict], *, start_time: str | None, end_time: str | None) -> list[dict]:
    return [
        row for row in ticks
        if _in_time_window(int(row.get("event_ts") or row.get("ingest_ts") or 0), start_time, end_time)
    ]


def _tick_time_range(ticks: list[dict]) -> dict | None:
    values = [int(row.get("event_ts") or row.get("ingest_ts") or 0) for row in ticks]
    values = [v for v in values if v > 0]
    if not values:
        return None
    return {
        "start": datetime.fromtimestamp(min(values) / 1000, tz=CN_TZ).isoformat(timespec="milliseconds"),
        "end": datetime.fromtimestamp(max(values) / 1000, tz=CN_TZ).isoformat(timespec="milliseconds"),
    }



def _normalize_symbols(symbols: list[str]) -> list[str]:
    """把回放输入统一成 app 内部 symbol, 避免 002607 这种裸代码静默匹配不到。"""
    out: list[str] = []
    seen = set()
    for symbol in symbols:
        normalized = _normalize_symbol(symbol)
        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out


def _normalize_symbol(symbol: str) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if "." in text:
        return text
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"}:
        return f"{text[2:]}.{text[:2]}"
    if len(text) == 6 and text.isdigit():
        suffix = _exchange_suffix(text)
        return f"{text}.{suffix}" if suffix else text
    return text


def _exchange_suffix(code: str) -> str | None:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("8", "43", "92")):
        return "BJ"
    return None


def _replay_rules(data_dir: Path) -> list[dict]:
    user_rules = []
    try:
        for rule in monitor_rules.load_all(data_dir):
            if rule.get("enabled") is False or rule.get("type") in {"strategy", "ladder"}:
                continue
            copied = dict(rule)
            copied["_replay_cooldown_seconds"] = rule.get("cooldown_seconds") or (REPLAY_MIN_EVENT_INTERVAL_MS // 1000)
            copied["cooldown_seconds"] = 0
            user_rules.append(copied)
    except Exception:
        user_rules = []
    if user_rules:
        return user_rules
    return [
        monitor_rules.normalize({
            "id": rid,
            "name": name,
            "type": "signal",
            "scope": "all",
            "conditions": [{"field": field, "op": "truth"}],
            "severity": "warn" if side in {"risk", "sell_risk"} else "info",
            "_replay_cooldown_seconds": REPLAY_MIN_EVENT_INTERVAL_MS // 1000,
            "cooldown_seconds": 0,
            "message": f"回放触发: {name}",
        })
        for rid, name, field, side in DEFAULT_REPLAY_RULES
    ]


def _rule_replay_cooldown_ms(rule: dict) -> int:
    raw = rule.get("_replay_cooldown_seconds")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = REPLAY_MIN_EVENT_INTERVAL_MS // 1000
    return max(0, seconds * 1000)


def _frame_to_monitor_row(frame: dict) -> dict:
    row = {
        "symbol": frame.get("symbol"),
        "name": frame.get("name"),
        "close": frame.get("latest_price"),
        "change_pct": frame.get("change_pct"),
        "amount": frame.get("amount"),
        "volume": frame.get("volume"),
        "vol_ratio_5d": frame.get("volume_ratio"),
        "ret_1m": frame.get("ret_1m"),
        "ret_3m": frame.get("ret_3m"),
        "ret_5m": frame.get("ret_5m"),
        "ret_15m": frame.get("ret_15m"),
        "amount_1m": frame.get("amount_1m"),
        "amount_5m": frame.get("amount_5m"),
        "amount_ratio_5m": frame.get("amount_ratio_5m"),
        "price_vs_vwap": frame.get("price_vs_vwap"),
    }
    for signal in frame.get("active_signals") or []:
        row[f"signal_{signal}"] = True
    for signal in frame.get("risk_flags") or []:
        row[f"signal_{signal}"] = True
    return row


def _event_signal(ev: dict) -> str:
    signals = ev.get("signals") or []
    if signals:
        return str(signals[0]).removeprefix("signal_")
    rid = str(ev.get("rule_id") or "")
    return rid.removeprefix("replay_") or str(ev.get("type") or "signal")


def _attach_outcome(event: dict, ticks: list[dict]) -> dict:
    price = _num(event.get("price"))
    ts = int(event.get("ts") or 0)
    if not price or not ts:
        event["returns"] = {}
        return event
    symbol_ticks = [
        row for row in ticks
        if row.get("symbol") == event.get("symbol") and int(row.get("event_ts") or row.get("ingest_ts") or 0) >= ts
    ]
    returns = {}
    for label, minutes in (("5m", 5), ("15m", 15), ("30m", 30), ("60m", 60)):
        target_ts = ts + minutes * 60_000
        target = _first_price_at_or_after(symbol_ticks, target_ts)
        returns[label] = (target - price) / price if target and price else None
    future_prices = [_num(row.get("last_price")) for row in symbol_ticks]
    future_prices = [p for p in future_prices if p is not None]
    event["returns"] = returns
    event["mfe"] = (max(future_prices) - price) / price if future_prices else None
    event["mae"] = (min(future_prices) - price) / price if future_prices else None
    return event


def _first_price_at_or_after(rows: list[dict], target_ts: int) -> float | None:
    for row in rows:
        ts = int(row.get("event_ts") or row.get("ingest_ts") or 0)
        if ts >= target_ts:
            return _num(row.get("last_price"))
    return None


def _summary(events: list[dict], *, key: str) -> list[dict]:
    grouped: dict[str, dict] = {}
    for ev in events:
        name = str(ev.get(key) or "unknown")
        item = grouped.setdefault(name, {"key": name, "count": 0, "avg_15m": None, "_vals_15m": []})
        item["count"] += 1
        ret_15m = (ev.get("returns") or {}).get("15m")
        if ret_15m is not None:
            item["_vals_15m"].append(ret_15m)
    out = []
    for item in grouped.values():
        vals = item.pop("_vals_15m")
        item["avg_15m"] = sum(vals) / len(vals) if vals else None
        out.append(item)
    return sorted(out, key=lambda r: r["count"], reverse=True)


def _in_time_window(ts: int, start_time: str | None, end_time: str | None) -> bool:
    if not start_time and not end_time:
        return True

    t = datetime.fromtimestamp(ts / 1000, tz=CN_TZ).time()
    if start_time and t < datetime.strptime(start_time, "%H:%M").time():
        return False
    return not (end_time and t > datetime.strptime(end_time, "%H:%M").time())


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
