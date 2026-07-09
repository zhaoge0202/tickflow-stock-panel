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
from app.services import manual_positions, quote_tick_store

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
    )
    support_distance = _distance(latest_price, nearest_support)
    resistance_distance = _distance(latest_price, nearest_resistance)
    score = _score(active_signals, risk_flags, pos, latest)
    reason_text = _reason_text(
        active_signals=active_signals,
        risk_flags=risk_flags,
        price=latest_price,
        vwap=vwap,
        support_distance=support_distance,
        resistance_distance=resistance_distance,
        position=pos,
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
        "quote_freshness": freshness,
        "source": (latest or {}).get("source") or "tdxapi",
        **_auction_summary(auction, prev_close),
        "position": pos,
        "levels": levels,
        **trade_summary,
    }


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
) -> tuple[list[str], list[str]]:
    signals: list[str] = []
    risks: list[str] = []
    auction_change = _num((auction or {}).get("auction_change_pct"))
    auction_unmatched_side = (auction or {}).get("auction_unmatched_side")
    auction_unmatched_volume = _num((auction or {}).get("auction_unmatched_volume")) or 0
    if auction_change is not None:
        if auction_change >= 0.02:
            signals.append("auction_strength")
        if auction_change <= -0.02:
            risks.append("auction_weakness")
    if auction_unmatched_side == "buy" and auction_unmatched_volume > 0:
        signals.append("auction_buy_imbalance")
    if auction_unmatched_side == "sell" and auction_unmatched_volume > 0:
        risks.append("auction_sell_imbalance")
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
) -> str:
    parts: list[str] = []
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


def _score(signals: list[str], risks: list[str], position: dict | None, latest: dict | None) -> int:
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
    if "large_order_selloff" in risks:
        score -= 30
    if "auction_weakness" in risks:
        score -= 20
    if "auction_sell_imbalance" in risks:
        score -= 10
    if "near_resistance" in risks:
        score -= 10
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
