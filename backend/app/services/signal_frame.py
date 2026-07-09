"""盘中 SignalFrame 构建。

SignalFrame 是决策队列和详情卡共用的解释快照。第一版以 tdxapi
quote_ticks、手动持仓、关键价位和现有 enriched 快照为输入。
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import date
from pathlib import Path

from app.indicators.levels import compute_levels
from app.market_time import cn_today
from app.services import manual_positions, market_breadth, quote_tick_store

logger = logging.getLogger(__name__)


def build_latest_frames(
    data_dir: Path,
    repo,
    *,
    symbols: list[str] | None = None,
    target_date: date | None = None,
    include_levels: bool = True,
    include_trade_summary: bool = False,
) -> list[dict]:
    target_date = target_date or cn_today()
    latest_rows = quote_tick_store.latest(data_dir, symbols, target_date=target_date)
    latest_by_symbol = {r["symbol"]: r for r in latest_rows}
    wanted = set(symbols or latest_by_symbol.keys())
    wanted.update(latest_by_symbol.keys())
    if not wanted:
        return []

    ticks_by_symbol: dict[str, list[dict]] = {}
    auction_by_symbol: dict[str, dict] = {}
    for row in quote_tick_store.read_ticks(data_dir, target_date=target_date, symbols=sorted(wanted)):
        if _is_auction_tick(row):
            sym = row["symbol"]
            prev = auction_by_symbol.get(sym)
            if prev is None or _row_ts(row) >= _row_ts(prev):
                auction_by_symbol[sym] = row
            continue
        ticks_by_symbol.setdefault(row["symbol"], []).append(row)

    return build_frames_from_tick_rows(
        data_dir,
        repo,
        ticks_by_symbol=ticks_by_symbol,
        latest_by_symbol=latest_by_symbol,
        auction_by_symbol=auction_by_symbol,
        symbols=sorted(wanted),
        target_date=target_date,
        include_levels=include_levels,
        include_trade_summary=include_trade_summary,
    )


def build_frames_from_tick_rows(
    data_dir: Path,
    repo,
    *,
    ticks_by_symbol: dict[str, list[dict]],
    latest_by_symbol: dict[str, dict] | None = None,
    auction_by_symbol: dict[str, dict] | None = None,
    symbols: list[str] | None = None,
    target_date: date | None = None,
    include_levels: bool = True,
    include_trade_summary: bool = False,
    market_context: dict | None = None,
) -> list[dict]:
    """基于已切好的 quote_ticks 构建 SignalFrame。

    盘中实时页与历史回放共用同一套 SignalFrame 口径。回放会逐时刻传入
    截止当时的 ticks, 避免用当前最新价污染历史判断。
    """
    latest_by_symbol = latest_by_symbol or {}
    auction_by_symbol = auction_by_symbol or {}
    wanted = set(symbols or ticks_by_symbol.keys() or latest_by_symbol.keys())
    wanted.update(ticks_by_symbol.keys())
    wanted.update(latest_by_symbol.keys())
    wanted.update(auction_by_symbol.keys())
    target_date = target_date or cn_today()
    enriched_by_symbol = _enriched_map(repo)
    name_map = _safe_name_map(repo, sorted(wanted))
    positions = manual_positions.by_symbol(data_dir, repo)
    market_context = _market_context(data_dir, market_context)
    out = []
    for symbol in sorted(wanted):
        latest = latest_by_symbol.get(symbol) or _latest_tick(ticks_by_symbol.get(symbol, []))
        enriched = enriched_by_symbol.get(symbol, {})
        levels = _levels(repo, symbol) if include_levels else {}
        trade_summary = _trade_tick_summary(data_dir, symbol, target_date) if include_trade_summary else {}
        frame = _build_one(
            symbol=symbol,
            name=(name_map.get(symbol) or latest.get("name")) if latest else name_map.get(symbol),
            latest=latest,
            ticks=ticks_by_symbol.get(symbol, []),
            auction=auction_by_symbol.get(symbol) or (latest if _is_auction_tick(latest) else None),
            enriched=enriched,
            position=positions.get(symbol),
            levels=levels,
            trade_summary=trade_summary,
            market_context=market_context,
        )
        if frame:
            out.append(frame)
    return out


def build_detail(
    data_dir: Path,
    repo,
    symbol: str,
    *,
    target_date: date | None = None,
) -> dict | None:
    frames = build_latest_frames(
        data_dir,
        repo,
        symbols=[symbol],
        target_date=target_date,
        include_levels=True,
        include_trade_summary=True,
    )
    if not frames:
        return None
    frame = frames[0]
    frame["bars_5s"] = quote_tick_store.bars(data_dir, symbol, freq="5s", target_date=target_date)
    frame["bars_1m"] = quote_tick_store.bars(data_dir, symbol, freq="1m", target_date=target_date)
    frame["bars_3m"] = quote_tick_store.bars(data_dir, symbol, freq="3m", target_date=target_date)
    frame["bars_5m"] = quote_tick_store.bars(data_dir, symbol, freq="5m", target_date=target_date)
    frame["bars_15m"] = quote_tick_store.bars(data_dir, symbol, freq="15m", target_date=target_date)
    return frame


def _build_one(
    *,
    symbol: str,
    name: str | None,
    latest: dict | None,
    ticks: list[dict],
    auction: dict | None,
    enriched: dict,
    position: dict | None,
    levels: dict,
    trade_summary: dict | None = None,
    market_context: dict | None = None,
) -> dict | None:
    latest_price = _num((latest or {}).get("last_price")) or _num(enriched.get("close"))
    if latest_price is None:
        return None
    prev_close = _num((latest or {}).get("prev_close")) or _num(enriched.get("prev_close"))
    change_pct = ((latest_price - prev_close) / prev_close) if prev_close else _num(enriched.get("change_pct"))
    amount = _num((latest or {}).get("amount")) or _num(enriched.get("amount"))
    volume = _num((latest or {}).get("volume")) or _num(enriched.get("volume"))
    high = _num((latest or {}).get("high")) or _num(enriched.get("high")) or latest_price
    low = _num((latest or {}).get("low")) or _num(enriched.get("low")) or latest_price
    open_price = _num((latest or {}).get("open")) or _num(enriched.get("open")) or latest_price
    vwap = _vwap(ticks, amount, volume)
    nearest_support, nearest_resistance = _nearest_levels(levels, latest_price)
    pos = manual_positions.enrich(position, latest_price)
    minute = _minute_metrics(ticks, latest_price, open_price)
    trade_summary = trade_summary or {}
    market_context = _market_context_fields(market_context)
    microstructure = _microstructure_metrics(
        latest=latest,
        ticks=ticks,
        enriched=enriched,
        latest_price=latest_price,
        prev_close=prev_close,
        amount=amount,
    )
    active_signals, risk_flags = _signals(
        latest_price=latest_price,
        open_price=open_price,
        high=high,
        low=low,
        vwap=vwap,
        ticks=ticks,
        minute=minute,
        trade_summary=trade_summary,
        support=nearest_support,
        resistance=nearest_resistance,
        position=pos,
        auction=auction,
        microstructure=microstructure,
        latest=latest,
        market_context=market_context,
    )
    support_distance = _distance(latest_price, nearest_support)
    resistance_distance = _distance(latest_price, nearest_resistance)
    score = _score(active_signals, risk_flags, pos, latest, microstructure=microstructure, market_context=market_context)
    reason_text = _reason_text(
        active_signals=active_signals,
        risk_flags=risk_flags,
        price=latest_price,
        vwap=vwap,
        support_distance=support_distance,
        resistance_distance=resistance_distance,
        position=pos,
        microstructure=microstructure,
        market_context=market_context,
    )
    now_ms = int(time.time() * 1000)
    ingest_ts = int((latest or {}).get("ingest_ts") or 0)
    freshness = "unknown"
    if ingest_ts:
        age = now_ms - ingest_ts
        freshness = "live" if age <= 15_000 else "stale" if age <= 300_000 else "snapshot"
    return {
        "symbol": symbol,
        "name": name,
        "ts": (latest or {}).get("event_ts") or now_ms,
        "price": latest_price,
        "latest_price": latest_price,
        "change_pct": change_pct,
        "amount": amount,
        "volume": volume,
        "volume_ratio": _num(enriched.get("vol_ratio_5d")),
        "vwap": vwap,
        "vwap_intraday": vwap,
        "price_vs_vwap": (latest_price - vwap) / vwap if vwap else None,
        "vwap_distance": (latest_price - vwap) / vwap if vwap else None,
        "open_range_position": _range_position(latest_price, ticks, open_price),
        **minute,
        "day_high_distance": (high - latest_price) / latest_price if latest_price else None,
        "day_low_distance": (latest_price - low) / latest_price if latest_price else None,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "support_distance": support_distance,
        "resistance_distance": resistance_distance,
        "active_signals": active_signals,
        "risk_flags": risk_flags,
        "decision_score": score,
        "reason_text": reason_text,
        "market_context": market_context,
        "market_status": market_context.get("status"),
        "market_temperature": market_context.get("market_temperature"),
        "market_risk_level": market_context.get("market_risk_level"),
        "market_up_count": market_context.get("up_count"),
        "market_down_count": market_context.get("down_count"),
        "market_up_down_ratio": market_context.get("up_down_ratio"),
        "major_index_change_pct": market_context.get("major_index_change_pct"),
        "market_context_text": market_context.get("text"),
        "microstructure": microstructure,
        "order_book": microstructure.get("order_book"),
        **_microstructure_flat_fields(microstructure),
        "quote_freshness": freshness,
        "source": (latest or {}).get("source") or "tdxapi",
        **_auction_summary(auction, prev_close),
        "position": pos,
        "levels": levels,
        **trade_summary,
    }


def _microstructure_metrics(
    *,
    latest: dict | None,
    ticks: list[dict],
    enriched: dict,
    latest_price: float,
    prev_close: float | None,
    amount: float | None,
) -> dict:
    latest = latest or {}
    order_book = _order_book(latest)
    bids = order_book["bids"]
    asks = order_book["asks"]
    bid_depth_amount = _num(latest.get("bid_depth_amount"))
    ask_depth_amount = _num(latest.get("ask_depth_amount"))
    if bid_depth_amount is None:
        bid_depth_amount = sum(_num(row.get("amount")) or 0 for row in bids) or None
    if ask_depth_amount is None:
        ask_depth_amount = sum(_num(row.get("amount")) or 0 for row in asks) or None
    bid_depth_vol = _num(latest.get("bid_depth_vol"))
    ask_depth_vol = _num(latest.get("ask_depth_vol"))
    if bid_depth_vol is None:
        bid_depth_vol = sum(_num(row.get("volume")) or 0 for row in bids) or None
    if ask_depth_vol is None:
        ask_depth_vol = sum(_num(row.get("volume")) or 0 for row in asks) or None

    spread = _num(latest.get("spread"))
    if spread is None and bids and asks:
        bid1 = _num(bids[0].get("price"))
        ask1 = _num(asks[0].get("price"))
        spread = ask1 - bid1 if bid1 is not None and ask1 is not None else None
    spread_pct = _num(latest.get("spread_pct"))
    if spread_pct is None and spread is not None and latest_price:
        spread_pct = spread / latest_price

    depth_imbalance = _num(latest.get("depth_imbalance"))
    if depth_imbalance is None and bid_depth_amount and ask_depth_amount:
        total = bid_depth_amount + ask_depth_amount
        depth_imbalance = (bid_depth_amount - ask_depth_amount) / total if total else None

    best_bid_amount = _num(latest.get("best_bid_amount")) or (_num(bids[0].get("amount")) if bids else None)
    best_ask_amount = _num(latest.get("best_ask_amount")) or (_num(asks[0].get("amount")) if asks else None)
    limit_up = _num(enriched.get("limit_up"))
    near_limit_up = (
        abs(latest_price - limit_up) / limit_up <= 0.001
        if limit_up
        else bool(prev_close and latest_price >= prev_close * 1.095)
    )
    limit_seal_amount = _num(latest.get("limit_seal_amount"))
    if limit_seal_amount is None and near_limit_up:
        limit_seal_amount = best_bid_amount
    seal_strength = None
    if limit_seal_amount is not None:
        if amount and amount > 0:
            seal_strength = limit_seal_amount / amount
        else:
            total_depth = (bid_depth_amount or 0) + (ask_depth_amount or 0)
            seal_strength = limit_seal_amount / total_depth if total_depth > 0 else None

    sell_wall = _nearest_wall(asks, latest_price, side="ask")
    buy_wall = _nearest_wall(bids, latest_price, side="bid")
    outside_inside_ratio = _num(latest.get("outside_inside_ratio"))
    outside_volume = _num(latest.get("outside_volume"))
    inside_volume = _num(latest.get("inside_volume"))
    if outside_inside_ratio is None and outside_volume is not None and inside_volume and inside_volume > 0:
        outside_inside_ratio = outside_volume / inside_volume
    active_net_volume = _num(latest.get("active_net_volume"))
    if active_net_volume is None and outside_volume is not None and inside_volume is not None:
        active_net_volume = outside_volume - inside_volume
    speed_rate = _num(latest.get("speed_rate"))
    score = 0.0
    if depth_imbalance is not None:
        score += depth_imbalance * 18
    if outside_inside_ratio is not None:
        score += max(min((outside_inside_ratio - 1.0) * 8, 8), -8)
    if speed_rate is not None:
        score += max(min(speed_rate * 4, 6), -6)
    if sell_wall.get("distance") is not None and 0 <= sell_wall["distance"] <= 0.005:
        score -= 6
    if buy_wall.get("distance") is not None and 0 <= buy_wall["distance"] <= 0.005:
        score += 4
    if seal_strength is not None:
        score += max(min(seal_strength * 10, 8), -8)

    out = {
        "status": "ready" if bids or asks else "missing",
        "order_book": order_book,
        "spread": spread,
        "spread_pct": spread_pct,
        "bid_depth_vol": bid_depth_vol,
        "ask_depth_vol": ask_depth_vol,
        "bid_depth_amount": bid_depth_amount,
        "ask_depth_amount": ask_depth_amount,
        "depth_imbalance": depth_imbalance,
        "best_bid_amount": best_bid_amount,
        "best_ask_amount": best_ask_amount,
        "limit_seal_amount": limit_seal_amount,
        "seal_strength": seal_strength,
        "nearest_sell_wall_price": sell_wall.get("price"),
        "sell_wall_distance": sell_wall.get("distance"),
        "nearest_buy_wall_price": buy_wall.get("price"),
        "buy_wall_distance": buy_wall.get("distance"),
        "current_volume": _num(latest.get("current_volume")),
        "inside_volume": inside_volume,
        "outside_volume": outside_volume,
        "outside_inside_ratio": outside_inside_ratio,
        "active_net_volume": active_net_volume,
        "speed_rate": speed_rate,
        "active1": _num(latest.get("active1")),
        "active2": _num(latest.get("active2")),
        "microstructure_score": max(min(score, 20), -20),
        "tick_count": len(ticks),
    }
    for side in ("bid", "ask"):
        levels = bids if side == "bid" else asks
        for idx in range(1, 6):
            row = levels[idx - 1] if len(levels) >= idx else {}
            out[f"{side}{idx}_price"] = row.get("price")
            out[f"{side}{idx}_vol"] = row.get("volume")
            out[f"{side}{idx}_amount"] = row.get("amount")
    return out


def _order_book(latest: dict) -> dict:
    return {
        "bids": [_book_level(latest, "bid", idx) for idx in range(1, 6) if _book_level(latest, "bid", idx)],
        "asks": [_book_level(latest, "ask", idx) for idx in range(1, 6) if _book_level(latest, "ask", idx)],
    }


def _book_level(latest: dict, side: str, idx: int) -> dict | None:
    price = _num(latest.get(f"{side}{idx}_price") if latest.get(f"{side}{idx}_price") is not None else latest.get(side + str(idx)))
    volume = _num(latest.get(f"{side}{idx}_vol"))
    if price is None and volume is None:
        return None
    amount = price * volume * 100.0 if price and volume else None
    return {
        "level": idx,
        "price": price,
        "volume": volume,
        "amount": amount,
    }


def _nearest_wall(levels: list[dict], latest_price: float, *, side: str) -> dict:
    amounts = [_num(row.get("amount")) for row in levels]
    amounts = [v for v in amounts if v is not None and v > 0]
    if not amounts:
        return {"price": None, "distance": None}
    avg_amount = statistics.fmean(amounts)
    threshold = max(avg_amount * 1.8, 200_000)
    candidates = [
        row for row in levels
        if (_num(row.get("amount")) or 0) >= threshold and _num(row.get("price")) is not None
    ]
    if not candidates:
        return {"price": None, "distance": None}
    if side == "ask":
        candidates.sort(key=lambda row: _num(row.get("price")) or float("inf"))
        price = _num(candidates[0].get("price"))
        distance = (price - latest_price) / latest_price if price and latest_price else None
    else:
        candidates.sort(key=lambda row: _num(row.get("price")) or 0, reverse=True)
        price = _num(candidates[0].get("price"))
        distance = (latest_price - price) / latest_price if price and latest_price else None
    return {"price": price, "distance": distance}


def _microstructure_flat_fields(microstructure: dict) -> dict:
    keys = [
        "spread", "spread_pct", "depth_imbalance", "bid_depth_amount", "ask_depth_amount",
        "bid_depth_vol", "ask_depth_vol", "best_bid_amount", "best_ask_amount",
        "limit_seal_amount", "seal_strength", "sell_wall_distance", "buy_wall_distance",
        "nearest_sell_wall_price", "nearest_buy_wall_price", "outside_inside_ratio",
        "active_net_volume", "speed_rate", "current_volume", "inside_volume",
        "outside_volume", "microstructure_score",
    ]
    for side in ("bid", "ask"):
        for idx in range(1, 6):
            keys.extend([f"{side}{idx}_price", f"{side}{idx}_vol", f"{side}{idx}_amount"])
    return {key: microstructure.get(key) for key in keys}


def _near_limit_up(latest_price: float, microstructure: dict) -> bool:
    seal_amount = _num(microstructure.get("limit_seal_amount"))
    if seal_amount is not None:
        return True
    best_bid = _num(microstructure.get("bid1_price"))
    return bool(best_bid and latest_price and abs(latest_price - best_bid) / latest_price <= 0.001)


def _market_context(data_dir: Path, snapshot: dict | None) -> dict:
    if snapshot is not None:
        return _market_context_fields(snapshot)
    try:
        return _market_context_fields(market_breadth.cached(data_dir))
    except Exception as e:
        logger.debug("SignalFrame 市场广度读取失败: %s", e)
        return _market_context_fields(market_breadth.unavailable(str(e)))


def _market_context_fields(snapshot: dict | None) -> dict:
    snapshot = snapshot or {}
    temperature = str(snapshot.get("market_temperature") or "unknown")
    up_down_ratio = _num(snapshot.get("up_down_ratio"))
    major_change = _num(snapshot.get("major_index_change_pct"))
    risk_level = _market_risk_level(temperature, up_down_ratio, major_change)
    out = {
        "source": snapshot.get("source") or "tdxapi",
        "status": snapshot.get("status") or "ready",
        "event_ts": snapshot.get("event_ts"),
        "ingest_ts": snapshot.get("ingest_ts"),
        "up_count": _num(snapshot.get("up_count")),
        "down_count": _num(snapshot.get("down_count")),
        "flat_count": _num(snapshot.get("flat_count")),
        "total_count": _num(snapshot.get("total_count")),
        "up_down_ratio": up_down_ratio,
        "market_temperature": temperature,
        "market_risk_level": risk_level,
        "major_index_change_pct": major_change,
        "major_indices": list(snapshot.get("major_indices") or [])[:5],
        "text": _market_context_text(snapshot, risk_level),
    }
    if snapshot.get("error"):
        out["error"] = str(snapshot.get("error"))
    return out


def _market_risk_level(temperature: str, up_down_ratio: float | None, major_change: float | None) -> str:
    if temperature == "cold" or (up_down_ratio is not None and up_down_ratio < 0.5):
        return "high"
    if major_change is not None and major_change <= -0.02:
        return "high"
    if temperature == "cool" or (up_down_ratio is not None and up_down_ratio < 0.8):
        return "elevated"
    if major_change is not None and major_change <= -0.01:
        return "elevated"
    if temperature in {"hot", "warm"}:
        return "supportive"
    return "neutral"


def _market_context_text(snapshot: dict, risk_level: str) -> str:
    temperature = str(snapshot.get("market_temperature") or "unknown")
    up_count = _num(snapshot.get("up_count"))
    down_count = _num(snapshot.get("down_count"))
    major_change = _num(snapshot.get("major_index_change_pct"))
    parts = []
    if risk_level in {"high", "elevated"}:
        parts.append("市场逆风")
    elif risk_level == "supportive":
        parts.append("市场偏暖")
    elif temperature != "unknown":
        parts.append("市场中性")
    else:
        parts.append("市场环境未知")
    if up_count is not None and down_count is not None:
        parts.append(f"上涨 {int(up_count)} / 下跌 {int(down_count)}")
    if major_change is not None:
        parts.append(f"核心指数 {major_change * 100:.1f}%")
    return ", ".join(parts)


def _market_headwind(market_context: dict | None) -> bool:
    if not market_context:
        return False
    risk_level = market_context.get("market_risk_level")
    if risk_level in {"high", "elevated"}:
        return True
    temperature = market_context.get("market_temperature")
    ratio = _num(market_context.get("up_down_ratio"))
    return bool(temperature in {"cool", "cold"} or (ratio is not None and ratio < 0.8))


def _market_tailwind(market_context: dict | None) -> bool:
    if not market_context:
        return False
    temperature = market_context.get("market_temperature")
    ratio = _num(market_context.get("up_down_ratio"))
    major_change = _num(market_context.get("major_index_change_pct"))
    if temperature not in {"hot", "warm"}:
        return False
    if ratio is not None and ratio < 1.1:
        return False
    return major_change is None or major_change > -0.005


def _chasing_signals(signals: list[str]) -> bool:
    return any(
        item in signals
        for item in ("open_range_breakout", "vwap_breakout", "speed_up", "intraday_new_high")
    )


def _signals(
    *,
    latest_price: float,
    open_price: float,
    high: float,
    low: float,
    vwap: float | None,
    ticks: list[dict],
    minute: dict,
    trade_summary: dict,
    support: float | None,
    resistance: float | None,
    position: dict | None,
    auction: dict | None = None,
    microstructure: dict | None = None,
    latest: dict | None = None,
    market_context: dict | None = None,
) -> tuple[list[str], list[str]]:
    signals: list[str] = []
    risks: list[str] = []
    microstructure = microstructure or {}
    auction_change = _num((auction or {}).get("auction_change_pct"))
    auction_unmatched_side = (auction or {}).get("auction_unmatched_side")
    auction_unmatched_volume = _num((auction or {}).get("auction_unmatched_volume")) or 0
    auction_unmatched_ratio = _num((auction or {}).get("auction_unmatched_ratio"))
    if auction_change is not None:
        if auction_change >= 0.02:
            signals.append("auction_strength")
        if auction_change <= -0.02:
            risks.append("auction_weakness")
    if auction_unmatched_side == "buy" and auction_unmatched_volume > 0:
        signals.append("auction_buy_imbalance")
        if auction_unmatched_ratio is not None and auction_unmatched_ratio >= 0.35:
            signals.append("auction_buy_pressure")
    if auction_unmatched_side == "sell" and auction_unmatched_volume > 0:
        risks.append("auction_sell_imbalance")
        if auction_unmatched_ratio is not None and auction_unmatched_ratio >= 0.35:
            risks.append("auction_sell_pressure")
    if vwap:
        if latest_price > vwap * 1.002:
            signals.append("vwap_breakout")
        if latest_price < vwap * 0.998:
            signals.append("vwap_breakdown")
            risks.append("below_vwap")
    open_high, open_low = _open_range(ticks, open_price)
    if open_high and latest_price > open_high * 1.002:
        signals.append("open_range_breakout")
    if open_low and latest_price < open_low * 0.998:
        signals.append("open_range_breakdown")
        risks.append("open_range_breakdown")
    if latest_price >= high:
        signals.append("intraday_new_high")
    if latest_price <= low:
        signals.append("intraday_new_low")
        risks.append("intraday_new_low")
    if resistance and 0 <= (resistance - latest_price) / latest_price <= 0.005:
        signals.append("near_resistance")
        risks.append("near_resistance")
    if support and 0 <= (latest_price - support) / latest_price <= 0.005:
        signals.append("pullback_near_support")
    if _amount_surge(ticks):
        signals.append("volume_surge_1m")
    if _num(minute.get("amount_ratio_5m")) and float(minute["amount_ratio_5m"]) >= 2:
        signals.append("volume_surge_5m")
    aggressive_buy_ratio = _num(trade_summary.get("aggressive_buy_ratio"))
    tick_net_amount = _num(trade_summary.get("tick_net_amount"))
    large_buy = _num(trade_summary.get("large_buy_amount")) or 0
    large_sell = _num(trade_summary.get("large_sell_amount")) or 0
    if aggressive_buy_ratio is not None and aggressive_buy_ratio >= 0.65 and (tick_net_amount or 0) > 0:
        signals.append("aggressive_buy_ratio_high")
    if large_buy > 0 and large_buy >= max(large_sell * 1.5, 100_000):
        signals.append("large_order_net_inflow")
    if large_sell > 0 and large_sell >= max(large_buy * 1.5, 100_000):
        signals.append("large_order_selloff")
        risks.append("large_order_selloff")
    depth_imbalance = _num(microstructure.get("depth_imbalance"))
    if depth_imbalance is not None:
        if depth_imbalance >= 0.35:
            signals.append("depth_bid_dominant")
        if depth_imbalance <= -0.35:
            signals.append("depth_ask_dominant")
            risks.append("depth_ask_dominant")
    spread_pct = _num(microstructure.get("spread_pct"))
    if spread_pct is not None and spread_pct >= 0.003:
        risks.append("wide_spread")
    total_depth = (_num(microstructure.get("bid_depth_amount")) or 0) + (_num(microstructure.get("ask_depth_amount")) or 0)
    if total_depth > 0 and total_depth < 500_000:
        risks.append("thin_liquidity")
    sell_wall_distance = _num(microstructure.get("sell_wall_distance"))
    if sell_wall_distance is not None and 0 <= sell_wall_distance <= 0.005:
        signals.append("sell_wall_nearby")
        risks.append("ask_wall_pressure")
    buy_wall_distance = _num(microstructure.get("buy_wall_distance"))
    if buy_wall_distance is not None and 0 <= buy_wall_distance <= 0.005:
        signals.append("buy_wall_support")
    outside_inside_ratio = _num(microstructure.get("outside_inside_ratio"))
    if outside_inside_ratio is not None:
        if outside_inside_ratio >= 1.5:
            signals.append("outside_disk_dominant")
        elif outside_inside_ratio <= 0.67:
            signals.append("inside_disk_dominant")
    speed_rate = _num(microstructure.get("speed_rate"))
    if speed_rate is not None:
        ret_1m = _num(minute.get("ret_1m")) or 0
        if speed_rate >= 0.5 or ret_1m >= 0.01:
            signals.append("speed_up")
            if depth_imbalance is not None and depth_imbalance < 0.1:
                risks.append("speed_up_without_depth")
        if speed_rate <= -0.5 or ret_1m <= -0.01:
            signals.append("speed_down")
            risks.append("speed_down")
    seal_strength = _num(microstructure.get("seal_strength"))
    if seal_strength is not None:
        if seal_strength >= 0.25:
            signals.append("seal_strengthening")
        elif seal_strength < 0.08 and _near_limit_up(latest_price, microstructure):
            signals.append("seal_weakening")
            risks.append("weak_seal")
    if latest and latest.get("ingest_ts") and int(time.time() * 1000) - int(latest["ingest_ts"]) > 15_000:
        risks.append("tdx_snapshot_stale")
    if _market_tailwind(market_context):
        signals.append("market_tailwind")
    if _market_headwind(market_context) and _chasing_signals(signals):
        risks.append("market_headwind")
        risks.append("market_breadth_weak")
    if position:
        level = position.get("risk_level")
        if level == "critical":
            signals.append("stop_loss_break")
            risks.append("stop_loss_break")
        elif level == "warn":
            signals.append("stop_loss_near")
            risks.append("stop_loss_near")
    return _unique(signals), _unique(risks)


def _reason_text(
    *,
    active_signals: list[str],
    risk_flags: list[str],
    price: float,
    vwap: float | None,
    support_distance: float | None,
    resistance_distance: float | None,
    position: dict | None,
    microstructure: dict | None = None,
    market_context: dict | None = None,
) -> str:
    parts: list[str] = []
    microstructure = microstructure or {}
    market_context = market_context or {}
    if "stop_loss_break" in risk_flags:
        parts.append("已跌破手动止损,请立即检查")
    elif "stop_loss_near" in risk_flags:
        parts.append("接近手动止损,需要确认是否处理")
    if "open_range_breakout" in active_signals:
        parts.append("放量突破开盘区间" if "volume_surge_1m" in active_signals else "突破开盘区间")
    if "volume_surge_5m" in active_signals:
        parts.append("5分钟成交额明显放大")
    if "large_order_net_inflow" in active_signals:
        parts.append("逐笔摘要显示大单净流入")
    if "large_order_selloff" in risk_flags:
        parts.append("逐笔摘要显示大单卖出压力")
    if "auction_strength" in active_signals:
        parts.append("集合竞价参考价明显高于昨收")
    if "auction_buy_imbalance" in active_signals:
        parts.append("集合竞价买方未匹配量占优")
    if "auction_weakness" in risk_flags:
        parts.append("集合竞价参考价明显低于昨收")
    if "auction_sell_imbalance" in risk_flags:
        parts.append("集合竞价卖方未匹配量占优")
    if "depth_bid_dominant" in active_signals:
        parts.append("买盘厚度占优")
    if "depth_ask_dominant" in risk_flags:
        parts.append("卖盘厚度占优")
    if "outside_disk_dominant" in active_signals:
        parts.append("外盘占优")
    if "inside_disk_dominant" in active_signals:
        parts.append("内盘占优")
    if "speed_up" in active_signals:
        parts.append("涨速走强")
    if "speed_down" in risk_flags:
        parts.append("下跌加速")
    if "ask_wall_pressure" in risk_flags:
        parts.append("上方卖墙较近")
    if "buy_wall_support" in active_signals:
        parts.append("下方买墙提供支撑")
    if "wide_spread" in risk_flags:
        parts.append("买卖价差偏宽")
    if "thin_liquidity" in risk_flags:
        parts.append("盘口偏薄")
    if "weak_seal" in risk_flags:
        parts.append("涨停附近封单偏弱")
    if "tdx_snapshot_stale" in risk_flags:
        parts.append("TDX 快照已滞后")
    if "market_breadth_weak" in risk_flags:
        parts.append("市场广度偏弱,追涨信号降级")
    elif "market_headwind" in risk_flags:
        parts.append(market_context.get("text") or "市场逆风")
    elif "market_tailwind" in active_signals:
        parts.append(market_context.get("text") or "市场偏暖")
    if "vwap_breakout" in active_signals and vwap:
        parts.append(f"当前价高于 VWAP {((price - vwap) / vwap) * 100:.1f}%")
    if "vwap_breakdown" in active_signals and vwap:
        parts.append(f"当前价低于 VWAP {((vwap - price) / vwap) * 100:.1f}%")
    if resistance_distance is not None and resistance_distance >= 0:
        parts.append(f"距离上方压力约 {resistance_distance * 100:.1f}%")
    if support_distance is not None and support_distance >= 0:
        parts.append(f"距离下方支撑约 {support_distance * 100:.1f}%")
    if position and position.get("position_action_hint"):
        parts.append(position["position_action_hint"])
    if not parts:
        parts.append("暂无强触发,继续观察价格、量能与关键价位")
    return ", ".join(parts)


def _score(
    signals: list[str],
    risks: list[str],
    position: dict | None,
    latest: dict | None,
    *,
    microstructure: dict | None = None,
    market_context: dict | None = None,
) -> int:
    score = 0
    if position and position.get("risk_level") == "critical":
        score += 100
    if position and position.get("risk_level") == "warn":
        score += 80
    if "stop_loss_break" in risks:
        score += 100
    if "open_range_breakout" in signals:
        score += 50
    if "pullback_near_support" in signals:
        score += 40
    if "volume_surge_1m" in signals:
        score += 30
    if "volume_surge_5m" in signals:
        score += 20
    if "large_order_net_inflow" in signals:
        score += 20
    if "auction_strength" in signals:
        score += 20
    if "auction_buy_imbalance" in signals:
        score += 10
    micro_score = _num((microstructure or {}).get("microstructure_score"))
    if micro_score is not None:
        score += int(max(min(micro_score, 15), -15))
    if "depth_bid_dominant" in signals:
        score += 8
    if "outside_disk_dominant" in signals:
        score += 5
    if "speed_up" in signals:
        score += 6
    if "market_tailwind" in signals:
        score += 5
    if "large_order_selloff" in risks:
        score -= 30
    if "auction_weakness" in risks:
        score -= 20
    if "auction_sell_imbalance" in risks:
        score -= 10
    if "near_resistance" in risks:
        score -= 10
    if "ask_wall_pressure" in risks:
        score -= 8
    if "wide_spread" in risks or "thin_liquidity" in risks:
        score -= 8
    if "tdx_snapshot_stale" in risks:
        score -= 20
    if "market_headwind" in risks:
        score -= 10
    if "market_breadth_weak" in risks:
        score -= 8
    if _market_headwind(market_context) and _chasing_signals(signals):
        score -= 12
    if latest and latest.get("ingest_ts") and int(time.time() * 1000) - int(latest["ingest_ts"]) > 60_000:
        score -= 40
    return max(0, min(100, score))


def _nearest_levels(levels: dict, price: float) -> tuple[float | None, float | None]:
    supports: list[float] = []
    resistances: list[float] = []
    for items in (levels or {}).values():
        for item in items or []:
            value = _num(item.get("value"))
            if value is None:
                continue
            side = item.get("side")
            if side == "support" or value < price:
                supports.append(value)
            if side == "resistance" or value > price:
                resistances.append(value)
    support = max((v for v in supports if v <= price), default=None)
    resistance = min((v for v in resistances if v >= price), default=None)
    return support, resistance


def _levels(repo, symbol: str) -> dict:
    if repo is None:
        return {}
    try:
        end = cn_today()
        start = date.fromordinal(end.toordinal() - 400)
        asset = repo.resolve_asset_type(symbol)
        df = repo.get_daily_asset(asset, symbol, start, end)
        if df.is_empty():
            return {}
        return compute_levels(df.tail(250))
    except Exception as e:
        logger.debug("SignalFrame 关键价位计算失败(%s): %s", symbol, e)
        return {}


def _enriched_map(repo) -> dict[str, dict]:
    if repo is None:
        return {}
    try:
        frames = []
        for asset in ("stock", "etf"):
            df, _ = repo.get_enriched_latest_asset(asset)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            df, _ = repo.get_enriched_latest()
            frames = [df] if not df.is_empty() else []
        out = {}
        for df in frames:
            for row in df.iter_rows(named=True):
                sym = row.get("symbol")
                if sym:
                    out[sym] = row
        return out
    except Exception:
        return {}


def _safe_name_map(repo, symbols: list[str]) -> dict[str, str]:
    if repo is None:
        return {}
    try:
        return repo.get_name_map(symbols)
    except Exception:
        return {}


def _auction_summary(auction: dict | None, prev_close: float | None) -> dict:
    if not auction:
        return {
            "auction_price": None,
            "auction_change_pct": None,
            "auction_matched_volume": None,
            "auction_unmatched_side": None,
            "auction_unmatched_volume": None,
            "auction_unmatched_ratio": None,
            "auction_pressure_score": None,
        }
    price = _num(auction.get("auction_price")) or _num(auction.get("last_price"))
    change_pct = _num(auction.get("auction_change_pct"))
    if change_pct is None and price is not None and prev_close:
        change_pct = (price - prev_close) / prev_close
    return {
        "auction_price": price,
        "auction_change_pct": change_pct,
        "auction_matched_volume": _num(auction.get("auction_matched_volume")),
        "auction_unmatched_side": auction.get("auction_unmatched_side"),
        "auction_unmatched_volume": _num(auction.get("auction_unmatched_volume")),
        "auction_unmatched_ratio": _num(auction.get("auction_unmatched_ratio")),
        "auction_pressure_score": _num(auction.get("auction_pressure_score")),
    }


def _is_auction_tick(row: dict | None) -> bool:
    return bool(row and row.get("price_type") == "auction_reference")


def _row_ts(row: dict | None) -> int:
    if not row:
        return 0
    return int(row.get("event_ts") or row.get("ingest_ts") or 0)


def _vwap(ticks: list[dict], amount: float | None, volume: float | None) -> float | None:
    if amount and volume and volume > 0:
        # tdx volume 为手, amount 通常为元; 若上游金额单位异常, VWAP 只作为辅助位置。
        candidate = amount / (volume * 100)
        if candidate > 0 and math.isfinite(candidate):
            return candidate
    vals = [_num(r.get("last_price")) for r in ticks if _num(r.get("last_price")) is not None]
    return statistics.fmean(vals) if vals else None


def _range_position(price: float, ticks: list[dict], open_price: float) -> float | None:
    open_high, open_low = _open_range(ticks, open_price)
    if open_high is None or open_low is None or open_high == open_low:
        return None
    return (price - open_low) / (open_high - open_low)


def _open_range(ticks: list[dict], open_price: float) -> tuple[float | None, float | None]:
    if not ticks:
        return open_price, open_price
    first = min(int(r.get("event_ts") or r.get("ingest_ts") or 0) for r in ticks)
    cutoff = first + 15 * 60 * 1000
    prices = [
        _num(r.get("last_price")) for r in ticks
        if int(r.get("event_ts") or r.get("ingest_ts") or 0) <= cutoff
    ]
    prices = [p for p in prices if p is not None]
    if not prices:
        return open_price, open_price
    return max(prices), min(prices)


def _amount_surge(ticks: list[dict]) -> bool:
    if len(ticks) < 4:
        return False
    rows = sorted(ticks, key=lambda r: r.get("event_ts") or r.get("ingest_ts") or 0)
    buckets: dict[int, float] = {}
    prev_amount = None
    for row in rows:
        ts = int(row.get("event_ts") or row.get("ingest_ts") or 0)
        amount = _num(row.get("amount"))
        if ts <= 0 or amount is None:
            continue
        delta = max(amount - prev_amount, 0) if prev_amount is not None else 0
        prev_amount = amount
        minute = ts // 60_000
        buckets[minute] = buckets.get(minute, 0.0) + delta
    if len(buckets) < 3:
        return False
    values = [v for _, v in sorted(buckets.items())]
    last = values[-1]
    base = statistics.fmean(values[:-1]) if values[:-1] else 0
    return base > 0 and last >= base * 2


def _minute_metrics(ticks: list[dict], latest_price: float, open_price: float) -> dict:
    rows = sorted(ticks, key=lambda r: r.get("event_ts") or r.get("ingest_ts") or 0)
    if not rows:
        return {
            "ret_1m": None,
            "ret_3m": None,
            "ret_5m": None,
            "ret_15m": None,
            "amount_1m": None,
            "amount_5m": None,
            "amount_ratio_5m": None,
            "open_range_high": open_price,
            "open_range_low": open_price,
            "intraday_high": latest_price,
            "intraday_low": latest_price,
        }
    latest_ts = max(int(r.get("event_ts") or r.get("ingest_ts") or 0) for r in rows)
    prices = [_num(r.get("last_price")) for r in rows if _num(r.get("last_price")) is not None]
    open_high, open_low = _open_range(rows, open_price)
    out = {
        "ret_1m": _window_return(rows, latest_price, latest_ts, 1),
        "ret_3m": _window_return(rows, latest_price, latest_ts, 3),
        "ret_5m": _window_return(rows, latest_price, latest_ts, 5),
        "ret_15m": _window_return(rows, latest_price, latest_ts, 15),
        "amount_1m": _amount_delta(rows, latest_ts, 1),
        "amount_5m": _amount_delta(rows, latest_ts, 5),
        "amount_ratio_5m": None,
        "open_range_high": open_high,
        "open_range_low": open_low,
        "intraday_high": max(prices) if prices else latest_price,
        "intraday_low": min(prices) if prices else latest_price,
    }
    base = _rolling_amount_base(rows, latest_ts, minutes=5, windows=3)
    if base and out["amount_5m"] is not None:
        out["amount_ratio_5m"] = out["amount_5m"] / base
    return out


def _window_return(rows: list[dict], latest_price: float, latest_ts: int, minutes: int) -> float | None:
    cutoff = latest_ts - minutes * 60_000
    candidates = [r for r in rows if int(r.get("event_ts") or r.get("ingest_ts") or 0) <= cutoff]
    ref = _num((candidates[-1] if candidates else rows[0]).get("last_price"))
    return (latest_price - ref) / ref if ref else None


def _amount_delta(rows: list[dict], latest_ts: int, minutes: int) -> float | None:
    cutoff = latest_ts - minutes * 60_000
    current = _num(rows[-1].get("amount"))
    if current is None:
        return None
    prior_rows = [r for r in rows if int(r.get("event_ts") or r.get("ingest_ts") or 0) <= cutoff]
    prior = _num((prior_rows[-1] if prior_rows else rows[0]).get("amount"))
    if prior is None:
        return None
    return max(current - prior, 0)


def _rolling_amount_base(rows: list[dict], latest_ts: int, *, minutes: int, windows: int) -> float | None:
    values = []
    for idx in range(1, windows + 1):
        end = latest_ts - idx * minutes * 60_000
        start = end - minutes * 60_000
        start_rows = [r for r in rows if int(r.get("event_ts") or r.get("ingest_ts") or 0) <= start]
        end_rows = [r for r in rows if int(r.get("event_ts") or r.get("ingest_ts") or 0) <= end]
        if not start_rows or not end_rows:
            continue
        start_amount = _num(start_rows[-1].get("amount"))
        end_amount = _num(end_rows[-1].get("amount"))
        if start_amount is not None and end_amount is not None:
            values.append(max(end_amount - start_amount, 0))
    values = [v for v in values if v > 0]
    return statistics.fmean(values) if values else None


def _trade_tick_summary(data_dir: Path, symbol: str, target_date: date) -> dict:
    rows = []
    try:
        from app.services.trade_tick_mysql import trade_tick_mysql_store

        if trade_tick_mysql_store.configured():
            rows = trade_tick_mysql_store.list_ticks(symbol, target_date, limit=500, order="desc")
    except Exception:
        rows = []
    if not rows:
        try:
            from app.plugins.tdxapi.provider import TDXAPIProvider

            provider = TDXAPIProvider()
            try:
                rows = provider.get_trade_ticks(symbol, target_date, mode="recent", limit=500)
            finally:
                provider.close()
        except Exception:
            rows = []
    return _summarize_trade_ticks(rows)


def _summarize_trade_ticks(rows: list[dict]) -> dict:
    buy_amount = 0.0
    sell_amount = 0.0
    large_buy = 0.0
    large_sell = 0.0
    large_count = 0
    for row in rows or []:
        amount = _num(row.get("amount")) or 0.0
        side = str(row.get("side") or "").lower()
        if side == "buy":
            buy_amount += amount
        elif side == "sell":
            sell_amount += amount
        if amount >= 100_000:
            large_count += 1
            if side == "buy":
                large_buy += amount
            elif side == "sell":
                large_sell += amount
    total = buy_amount + sell_amount
    return {
        "tick_buy_amount": buy_amount if rows else None,
        "tick_sell_amount": sell_amount if rows else None,
        "tick_net_amount": (buy_amount - sell_amount) if rows else None,
        "large_buy_amount": large_buy if rows else None,
        "large_sell_amount": large_sell if rows else None,
        "aggressive_buy_ratio": buy_amount / total if total > 0 else None,
        "large_order_count": large_count if rows else None,
        "tick_sample_count": len(rows or []),
    }


def _distance(price: float, level: float | None) -> float | None:
    if level is None or not price:
        return None
    return abs(price - level) / price


def _latest_tick(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(rows, key=lambda r: (r.get("event_ts") or 0, r.get("ingest_ts") or 0))


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _unique(items: list[str]) -> list[str]:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
