"""异动边缘统计 — 按交易所异动规则口径实时计算个股接近度。

规则 (近似口径, 与交易所《交易规则》的异常波动/严重异常波动披露阈值对齐;
主板/科创板条款号指上交所《交易规则(2026年修订)》, 2026-07-06 施行):
- 主板:     连续3日收盘价涨跌幅偏离值累计 ±20% (5.4.2)
- 创业板/科创板: 3日 ±30% (科创板 6.10)
- 北交所:   3日 ±40%
- 严重异常波动 (5.4.3/6.11): 10日累计偏离 +100%(-50%), 30日 +200%(-70%) —
  负向阈值显著严于正向 (跌方向更早触发), 各板块相同。
  「10日内4次同向异常波动」情形 (科创板3次) 需事件计数, 暂未实现。
- 风险警示 (ST/*ST): 2026-07-06 起主板风险警示股票涨跌幅限制调整为 10%,
  异常波动特别规定 (原 3日±15% / 10日+50% / 30日+100%) 同步废止,
  与主板普通股票适用同一套标准 (见 price_limits.MAIN_BOARD_ST_LIMIT_CHANGE_DATE)。

偏离值 = 个股 N 日累计涨跌幅 - 对应指数同期涨跌幅 (enriched 运行时列 deviate_Nd)。
「接近度」= |实时偏离| / 该方向阈值: ≥1 已触发, ≥0.7 边缘, ≥0.5 观察。
盘中实时叠加: 历史偏离 (已完成交易日) + 今日实时涨跌 - 基准指数今日涨跌。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl

from app.indicators.pipeline import DEVIATION_WINDOWS

# ── 规则表 ────────────────────────────────────────────────

@dataclass(frozen=True)
class AbnormalRule:
    board: str
    st: bool
    # 各窗口阈值 (小数): {窗口: (正向, 负向)} — 严重异动负向阈值更严 (见模块 docstring)
    thresholds: dict[int, tuple[float, float]]


# 3日异常波动阈值各板块对称; 10/30日严重异动各板块一致且不对称 (+100%/-50%, +200%/-70%)
_MAIN = {3: (0.20, 0.20), 10: (1.00, 0.50), 30: (2.00, 0.70)}
_GEM_STAR = {3: (0.30, 0.30), 10: (1.00, 0.50), 30: (2.00, 0.70)}
_BSE = {3: (0.40, 0.40), 10: (1.00, 0.50), 30: (2.00, 0.70)}

RULES_META: list[dict[str, Any]] = [
    {"board": "主板", "st": False, "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in _MAIN.items()},
     "note": "3日±20% 异常波动; 严重异常波动 10日+100%(-50%) / 30日+200%(-70%), "
             "负向更严; 2026-07-06 起风险警示(ST)股票同口径 (原±15%特别规定已废止)"},
    {"board": "创业板/科创板", "st": False, "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in _GEM_STAR.items()},
     "note": "20%涨跌幅板块, 3日±30%"},
    {"board": "北交所", "st": False, "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in _BSE.items()},
     "note": "30%涨跌幅板块, 3日±40%"},
]

_BENCH_RT_CANDIDATES = ["000002.SH", "000001.SH", "399107.SZ", "399001.SZ", "899050.BJ"]


def board_of(symbol: str) -> str:
    """按代码前缀判定板块。"""
    code = symbol.split(".")[0]
    if symbol.endswith(".BJ") or code[:2] in {"43", "83", "87", "92"}:
        return "北交所"
    if code.startswith("68"):
        return "科创板"
    if code.startswith(("30", "301")):
        return "创业板"
    return "主板"


def is_st_name(name: str | None) -> bool:
    return bool(name) and "ST" in str(name).upper()


def rule_for(symbol: str, name: str | None) -> AbnormalRule:
    board = board_of(symbol)
    st = is_st_name(name)
    # 主板风险警示股票 2026-07-06 起与普通股票同标准 (涨跌幅 10%,
    # 异常波动特别规定废止); st 仅为展示标记。创业板/科创板/北交所本就不区分。
    if board == "北交所":
        return AbnormalRule(board, st, _BSE)
    if board in ("创业板", "科创板"):
        return AbnormalRule(board, st, _GEM_STAR)
    return AbnormalRule(board, st, _MAIN)


# ── 快照计算 ──────────────────────────────────────────────

_hist_cache_lock = threading.Lock()
_hist_cache: dict[str, Any] = {}
_HIST_CACHE_TTL = 60.0

_STATUS_TRIGGERED = "triggered"
_STATUS_EDGE = "edge"
_STATUS_WATCH = "watch"


def _status_of(closeness: float) -> str:
    if closeness >= 1.0:
        return _STATUS_TRIGGERED
    if closeness >= 0.7:
        return _STATUS_EDGE
    return _STATUS_WATCH


def _hist_snapshot(repo: Any) -> dict[str, Any]:
    """enriched 最新日的偏离列快照 (60s 进程内缓存)。"""
    now = time.monotonic()
    with _hist_cache_lock:
        cached = _hist_cache.get("data")
        if cached is not None and now - cached["_ts"] < _HIST_CACHE_TTL:
            return cached

    df, cache_date = repo.get_enriched_latest()
    rows: dict[str, dict[str, Any]] = {}
    if not df.is_empty() and "symbol" in df.columns:
        cols = ["symbol", *[c for c in ("name", "close", "change_pct",
                                        "deviate_3d", "deviate_10d", "deviate_30d") if c in df.columns]]
        df = df.select(cols)
        for r in df.iter_rows(named=True):
            rows[str(r["symbol"])] = {
                "name": r.get("name"),
                "close": r.get("close"),
                "rt_pct": r.get("change_pct"),
                "deviate_3d": r.get("deviate_3d"),
                "deviate_10d": r.get("deviate_10d"),
                "deviate_30d": r.get("deviate_30d"),
            }
    payload = {"_ts": now, "rows": rows, "cache_date": cache_date.isoformat() if cache_date else None}
    with _hist_cache_lock:
        _hist_cache["data"] = payload
    return payload


def _bench_rt_pct(quote_service: Any) -> float:
    """基准指数今日实时涨跌 (各候选均值, 缺数据时 0)。"""
    try:
        df = quote_service.get_index_quotes()
    except Exception:
        return 0.0
    if df is None or df.is_empty():
        return 0.0
    df = df.filter(pl.col("symbol").is_in(_BENCH_RT_CANDIDATES))
    if df.is_empty():
        return 0.0
    for col in ("change_pct", "pct", "pct_change"):
        if col in df.columns:
            vals = df[col].drop_nulls()
            if vals.len() > 0:
                return float(vals.mean())
    if {"close", "prev_close"} <= set(df.columns):
        sub = df.select(["close", "prev_close"]).drop_nulls()
        if sub.height > 0:
            return float((sub["close"] / sub["prev_close"] - 1).mean())
    return 0.0


def build_overview(
    repo: Any,
    quote_service: Any = None,
    *,
    min_closeness: float = 0.5,
    limit: int = 200,
) -> dict[str, Any]:
    """返回异动边缘总览: 规则表 + 按接近度排序的个股列表。"""
    hist = _hist_snapshot(repo)
    cache_date = hist.get("cache_date")
    hist_rows: dict[str, dict[str, Any]] = hist["rows"]

    bench_rt = _bench_rt_pct(quote_service) if quote_service is not None else 0.0
    # enriched 已含今日收盘 (盘后已同步) 时, 今日涨跌已计入历史偏离, 不再叠加
    includes_today = cache_date is not None and cache_date >= date.today().isoformat()

    out_rows: list[dict[str, Any]] = []
    for symbol, base in hist_rows.items():
        rule = rule_for(symbol, base.get("name"))
        rt_pct = base.get("rt_pct")
        rt_delta = 0.0 if includes_today else ((rt_pct or 0.0) - bench_rt)

        windows: dict[str, dict[str, Any]] = {}
        max_closeness = 0.0
        for n in DEVIATION_WINDOWS:
            hist_dev = base.get(f"deviate_{n}d")
            if hist_dev is None:
                continue
            live = hist_dev + rt_delta
            up_t, down_t = rule.thresholds[n]
            threshold = up_t if live >= 0 else down_t
            closeness = abs(live) / threshold if threshold > 0 else 0.0
            windows[f"{n}d"] = {
                "value": round(live, 4),
                "threshold": threshold,
                "closeness": round(closeness, 4),
            }
            max_closeness = max(max_closeness, closeness)
        if not windows or max_closeness < min_closeness:
            continue
        out_rows.append({
            "symbol": symbol,
            "name": base.get("name"),
            "board": rule.board,
            "st": rule.st,
            "close": base.get("close"),
            "rt_pct": rt_pct,
            "windows": windows,
            "max_closeness": round(max_closeness, 4),
            "status": _status_of(max_closeness),
        })

    out_rows.sort(key=lambda r: r["max_closeness"], reverse=True)
    counts = {
        _STATUS_TRIGGERED: sum(1 for r in out_rows if r["status"] == _STATUS_TRIGGERED),
        _STATUS_EDGE: sum(1 for r in out_rows if r["status"] == _STATUS_EDGE),
        _STATUS_WATCH: sum(1 for r in out_rows if r["status"] == _STATUS_WATCH),
    }
    return {
        "asof": time.time(),
        "cache_date": cache_date,
        "bench_rt_pct": round(bench_rt, 4),
        "includes_today": includes_today,
        "rules": RULES_META,
        "counts": counts,
        "rows": out_rows[:limit],
    }
