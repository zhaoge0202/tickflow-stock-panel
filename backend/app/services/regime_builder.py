"""市场环境(regime)计算 — 纯函数模块。

职责: 从已算好的 enriched 数据(含信号列)按日聚合环境指标, 用规则引擎分类离散状态,
持久化为时序表。不重算指标(不走 compute_indicators), 不依赖 quote/depth service。

性能设计:
- run_regime_batch 用 polars group_by("date").agg(...) 一次聚合多日, 非逐日循环。
- 数据走 repo.get_enriched_range(内存缓存, 已含信号列); 缓存不覆盖时走 scan_parquet 慢路径。

与 market_overview_builder 的区别:
- overview 面向单日详情(实时总览), 重算指标。
- regime 面向多日聚合统计(时序分析), 只聚合不重算。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# ───────────────────────── 状态分类阈值(可调) ─────────────────────────
# 各维度子分用归一化映射(线性插值到 0-100), 再加权求和。
# 综合 = 赚钱效应×0.4 + 指数趋势×0.3 + 板块结构×0.2 + 活跃度×0.1

WEIGHTS = {
    "money_effect": 0.4,   # 赚钱效应(涨停/封板率/涨跌比)
    "index_trend": 0.3,    # 指数趋势(指数涨幅/MA20上方占比)
    "board_structure": 0.2,  # 板块结构(涨跌离散度, 简化)
    "activity": 0.1,       # 活跃度(成交额/换手)
}

# 离散状态阈值(综合分)
STATE_STRONG = 75       # >= 强势
STATE_LEAN_STRONG = 60  # 60-75 偏强
STATE_RANGE = 40        # 40-60 震荡
STATE_LEAN_WEAK = 25    # 25-40 偏弱
# < 25 弱势

# 归一化映射的参考点(线性插值 0-100)
_LIN = {
    "limit_up": [(0, 0), (15, 40), (30, 70), (50, 100)],       # 涨停数
    "seal_rate": [(0.3, 0), (0.5, 40), (0.7, 70), (0.9, 100)],  # 封板率
    "up_ratio": [(0.4, 0), (1.0, 40), (2.0, 70), (3.0, 100)],   # 涨跌比
    "index_pct": [(-0.02, 0), (0.0, 40), (0.01, 70), (0.02, 100)],  # 指数涨幅
    "above_ma20": [(0.3, 0), (0.5, 40), (0.6, 70), (0.8, 100)],  # MA20上方占比
    "amount": [(0.5e11, 0), (1e11, 40), (1.5e11, 70), (2.5e11, 100)],  # 成交额
}

STATE_LABELS = {
    "strong": "强势",
    "lean_strong": "偏强",
    "range": "震荡",
    "lean_weak": "偏弱",
    "weak": "弱势",
}


def _linear_score(value: float, points: list[tuple[float, float]]) -> float:
    """分段线性插值。points 是 [(输入值, 输出分)] 升序列表。"""
    if value <= points[0][0]:
        return float(points[0][1])
    if value >= points[-1][0]:
        return float(points[-1][1])
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= value <= x1:
            if x1 == x0:
                return float(y0)
            return float(y0 + (y1 - y0) * (value - x0) / (x1 - x0))
    return float(points[-1][1])


def classify_state(metrics: dict) -> tuple[str, int]:
    """规则引擎: 多维指标 → 离散状态 + 综合分(0-100)。

    各维度子分加权: 赚钱效应(涨停数/封板率/涨跌比) + 指数趋势(涨幅/MA20)
    + 板块结构(涨跌离散度简化) + 活跃度(成交额)。
    """
    # 赚钱效应子分 = 涨停/封板率/涨跌比 三者平均
    limit_up = metrics.get("limit_up", 0) or 0
    seal_rate = metrics.get("seal_rate", 0.5) or 0.5
    up_ratio = metrics.get("up_ratio", 1.0) or 1.0
    money = (
        _linear_score(limit_up, _LIN["limit_up"])
        + _linear_score(seal_rate, _LIN["seal_rate"])
        + _linear_score(up_ratio, _LIN["up_ratio"])
    ) / 3

    index_pct = metrics.get("index_pct", 0.0) or 0.0
    above_ma20 = metrics.get("above_ma20_pct", 0.5) or 0.5
    index_trend = (
        _linear_score(index_pct, _LIN["index_pct"])
        + _linear_score(above_ma20, _LIN["above_ma20"])
    ) / 2

    # 板块结构: 用涨跌家数比的偏离度简化(涨跌越均衡=震荡, 极端=方向明确)
    # up_ratio 接近 1 → 震荡(中分); 远离 1 → 方向明确(高低分看方向)
    # 已在 money_effect 的 up_ratio 体现, 这里用涨停+跌停的对比做补充
    limit_down = metrics.get("limit_down", 0) or 0
    if limit_up + limit_down > 0:
        board = (limit_up - limit_down) / max(limit_up + limit_down, 1) * 50 + 50
    else:
        board = 50.0

    total_amount = metrics.get("total_amount", 1e11) or 1e11
    activity = _linear_score(total_amount, _LIN["amount"])

    score = (
        money * WEIGHTS["money_effect"]
        + index_trend * WEIGHTS["index_trend"]
        + board * WEIGHTS["board_structure"]
        + activity * WEIGHTS["activity"]
    )
    score = max(0, min(100, round(score)))

    if score >= STATE_STRONG:
        state = "strong"
    elif score >= STATE_LEAN_STRONG:
        state = "lean_strong"
    elif score >= STATE_RANGE:
        state = "range"
    elif score >= STATE_LEAN_WEAK:
        state = "lean_weak"
    else:
        state = "weak"
    return state, score


# ───────────────────────── 批量聚合 ─────────────────────────

def _aggregate_daily(df: pl.DataFrame, index_pct_map: dict | None = None) -> pl.DataFrame:
    """对多日多 symbol 的 enriched DataFrame 按 date 聚合环境指标。

    纯 polars 聚合, 不重算指标(假设 df 已含 signal_*/change_pct/ma20 等列)。
    index_pct_map: {date: 指数涨幅} 可选, 由调用方从指数数据预先算好。
    """
    needed = ["date", "change_pct", "amount", "signal_limit_up",
              "signal_limit_down", "signal_broken_limit_up",
              "consecutive_limit_ups", "close", "ma20"]
    avail = [c for c in needed if c in df.columns]
    if "date" not in avail or "change_pct" not in avail:
        return pl.DataFrame()

    # 基础聚合
    agg_exprs = []
    if "change_pct" in avail:
        agg_exprs.append(pl.col("change_pct"))
    grouped = df.group_by("date").agg(
        *[
            pl.col("change_pct").gt(0).sum().alias("up_count")
            if "change_pct" in avail else pl.lit(0).alias("up_count"),
            pl.col("change_pct").lt(0).sum().alias("down_count")
            if "change_pct" in avail else pl.lit(0).alias("down_count"),
            pl.len().alias("total_count"),
        ],
        *(
            [pl.col("signal_limit_up").cast(pl.Boolean).sum().alias("limit_up")]
            if "signal_limit_up" in avail else [pl.lit(0).alias("limit_up")]
        ),
        *(
            [pl.col("signal_limit_down").cast(pl.Boolean).sum().alias("limit_down")]
            if "signal_limit_down" in avail else [pl.lit(0).alias("limit_down")]
        ),
        *(
            [pl.col("signal_broken_limit_up").cast(pl.Boolean).sum().alias("broken_limit")]
            if "signal_broken_limit_up" in avail else [pl.lit(0).alias("broken_limit")]
        ),
        *(
            [pl.col("consecutive_limit_ups").max().alias("max_consecutive")]
            if "consecutive_limit_ups" in avail else [pl.lit(0).alias("max_consecutive")]
        ),
        *(
            [pl.col("amount").sum().alias("total_amount")]
            if "amount" in avail else [pl.lit(0).alias("total_amount")]
        ),
        *(
            [pl.col("amount").mean().alias("avg_amount")]
            if "amount" in avail else [pl.lit(0).alias("avg_amount")]
        ),
    ).sort("date")

    # 转成 dict 列表做后续计算(polars 表达式难表达的比率/MA20占比/分类)
    index_pct_map = index_pct_map or {}
    rows = []
    for r in grouped.iter_rows(named=True):
        up = r.get("up_count", 0) or 0
        down = r.get("down_count", 0) or 0
        limit_up = r.get("limit_up", 0) or 0
        broken = r.get("broken_limit", 0) or 0
        # MA20 上方占比
        ma20_above = 0
        if "close" in avail and "ma20" in avail:
            day_df = df.filter(pl.col("date") == r["date"])
            if not day_df.is_empty() and "ma20" in day_df.columns:
                valid = day_df.filter(pl.col("ma20").is_not_null() & (pl.col("ma20") > 0))
                if not valid.is_empty():
                    above = valid.filter(pl.col("close") > pl.col("ma20"))
                    ma20_above = above.height / valid.height
        metrics = {
            "limit_up": limit_up,
            "limit_down": r.get("limit_down", 0) or 0,
            "broken_limit": broken,
            "max_consecutive": r.get("max_consecutive", 0) or 0,
            "seal_rate": (limit_up / (limit_up + broken)) if (limit_up + broken) > 0 else 0.5,
            "up_count": up,
            "down_count": down,
            "up_ratio": (up / down) if down > 0 else (float(up) if up > 0 else 1.0),
            "index_pct": index_pct_map.get(r["date"], 0.0),
            "above_ma20_pct": ma20_above,
            "total_amount": r.get("total_amount", 0) or 0,
            "avg_turnover": r.get("avg_amount", 0) or 0,
        }
        state, score = classify_state(metrics)
        rows.append({
            "date": r["date"],
            "state": state,
            "score": score,
            "limit_up": limit_up,
            "limit_down": metrics["limit_down"],
            "broken_limit": broken,
            "max_consecutive": metrics["max_consecutive"],
            "seal_rate": round(metrics["seal_rate"], 4),
            "up_count": up,
            "down_count": down,
            "up_ratio": round(metrics["up_ratio"], 4),
            "index_pct": round(metrics["index_pct"], 4),
            "above_ma20_pct": round(ma20_above, 4),
            "total_amount": metrics["total_amount"],
            "avg_turnover": metrics["avg_turnover"],
        })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _scan_enriched_fallback(repo, start: date, end: date) -> pl.DataFrame | None:
    """缓存不覆盖时的慢路径: 一次性 scan 全部 enriched parquet + 重算指标。

    仅在 regime 首次全量回填或缓存未预热时触发。返回含信号列的多日 DataFrame。
    """
    try:
        enriched_dir = repo.store.data_dir / "kline_daily_enriched"
        if not enriched_dir.exists():
            return None
        from app.indicators.pipeline import compute_indicators, compute_limit_signals
        df = pl.scan_parquet(enriched_dir / "**" / "*.parquet").filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
        ).collect()
        if df.is_empty():
            return None
        df = compute_indicators(df)
        df = compute_limit_signals(df)
        return df
    except Exception as e:  # noqa: BLE001
        logger.warning("regime scan_enriched_fallback failed: %s", e)
        return None


def _load_index_pct(repo, start: date, end: date, symbol: str = "000001.SH") -> dict:
    """读取主力指数日K, 算每日涨幅 → {date: pct}。指数数量少, 单次读取可接受。"""
    try:
        df = repo.get_index_daily(symbol, start, end, columns=["date", "change_pct"])
        if df.is_empty() or "change_pct" not in df.columns:
            return {}
        return {r["date"]: float(r["change_pct"] or 0) for r in df.iter_rows(named=True)}
    except Exception as e:  # noqa: BLE001
        logger.warning("regime load_index_pct failed: %s", e)
        return {}


def run_regime_batch(repo, start: date, end: date) -> pl.DataFrame:
    """批算 [start, end] 的环境时序。

    性能: 优先 repo.get_enriched_range(内存缓存); 缓存不覆盖走 scan_parquet 慢路径。
    按 date group_by 聚合, 不逐日重算。返回完整时序 DataFrame(可能为空)。
    """
    if start > end:
        return pl.DataFrame()

    # 指数涨幅(主力指数)
    index_pct_map = _load_index_pct(repo, start, end)

    # enriched 多日数据(优先缓存)
    df = repo.get_enriched_range(start, end)
    if df is None or df.is_empty():
        logger.info("regime batch: enriched cache miss [%s~%s], fallback to scan", start, end)
        df = _scan_enriched_fallback(repo, start, end)
    if df is None or df.is_empty():
        logger.info("regime batch: no enriched data for [%s~%s]", start, end)
        return pl.DataFrame()

    return _aggregate_daily(df, index_pct_map)


# ───────────────────────── 持久化(upsert) ─────────────────────────

REGIME_DIR = "regime_history"


def regime_path(data_dir: Path) -> Path:
    return data_dir / REGIME_DIR / "part.parquet"


def load_regime_history(data_dir: Path) -> pl.DataFrame:
    """读取全部 regime 时序; 不存在返回空 DataFrame。"""
    p = regime_path(data_dir)
    if not p.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("load_regime_history failed: %s", e)
        return pl.DataFrame()


def upsert_regime_history(data_dir: Path, new_rows: pl.DataFrame) -> None:
    """按 date 覆盖(upsert): 重算的天覆盖旧行, 新天追加。

    读旧 → anti-join 掉 new_rows 的天 → concat new_rows → 排序 → 写回。
    """
    if new_rows.is_empty() or "date" not in new_rows.columns:
        return
    p = regime_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_dates = set(new_rows["date"].to_list())
    old = load_regime_history(data_dir)
    if old.is_empty():
        combined = new_rows
    else:
        kept = old.filter(~pl.col("date").is_in(list(new_dates)))
        combined = pl.concat([kept, new_rows], how="vertical_relaxed")
    combined = combined.sort("date").unique(subset=["date"], keep="last")
    combined.write_parquet(p)


def get_regime_coverage(data_dir: Path) -> dict:
    """返回 regime 时序的覆盖元信息(供数据画像/API)。"""
    df = load_regime_history(data_dir)
    if df.is_empty():
        return {"rows": 0, "earliest_date": None, "latest_date": None}
    return {
        "rows": df.height,
        "earliest_date": str(df["date"].min()),
        "latest_date": str(df["date"].max()),
    }


def detect_stale_dates(data_dir: Path, repo) -> list[date]:
    """检测 regime 已有但需要重算的天(enriched 被覆写)。

    用 mtime 比对: enriched 分区 parquet 的 mtime > regime parquet 的 mtime
    → 该日 enriched 更新过, regime 需重算。
    """
    regime_p = regime_path(data_dir)
    if not regime_p.exists():
        return []
    regime_mtime = regime_p.stat().st_mtime
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return []
    stale: list[date] = []
    existing = load_regime_history(data_dir)
    if existing.is_empty():
        return []
    existing_dates = set(existing["date"].to_list())
    for part in enriched_dir.glob("date=*/part.parquet"):
        try:
            ds = part.parent.name.replace("date=", "")
            d = date.fromisoformat(ds)
        except (ValueError, OSError):
            continue
        if d not in existing_dates:
            continue
        try:
            if part.stat().st_mtime > regime_mtime:
                stale.append(d)
        except OSError:
            continue
    return sorted(stale)


def compute_regime_incremental(repo, data_dir: Path, *, today: date | None = None) -> pl.DataFrame:
    """增量计算 regime(供 daily_pipeline / 启动补算调用)。

    双检测: 1) 缺口(enriched 有但 regime 没有) 2) stale(enriched 被覆写)。
    自动补齐所有需要的日。返回本次新算的 DataFrame。
    """
    today = today or date.today()
    existing = load_regime_history(data_dir)

    # 缺口: enriched 有哪些天, regime 缺哪些
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_dates: set[date] = set()
    if enriched_dir.exists():
        for part in enriched_dir.glob("date=*/part.parquet"):
            try:
                ds = part.parent.name.replace("date=", "")
                enriched_dates.add(date.fromisoformat(ds))
            except ValueError:
                continue
    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    missing = sorted(d for d in enriched_dates if d not in existing_dates and d <= today)

    # stale: enriched 覆写过
    stale = detect_stale_dates(data_dir, repo)

    to_compute = sorted(set(missing) | set(stale))
    if not to_compute:
        logger.debug("regime incremental: nothing to compute")
        return pl.DataFrame()

    logger.info("regime incremental: compute %d days (missing=%d, stale=%d)",
                len(to_compute), len(missing), len(stale))
    new_rows = run_regime_batch(repo, start=to_compute[0], end=to_compute[-1])
    if not new_rows.is_empty():
        upsert_regime_history(data_dir, new_rows)
    return new_rows
