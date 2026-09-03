"""Preselect candidates for next-day auction confirmation."""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.market_time import cn_today
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine

CN_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PRESELECT_STRATEGY_IDS = (
    "custom_dual_edge",
    "custom_dual_edge_focus",
    "custom_dual_edge_v3",
)
# 当前盘后预选算法是双刃合专属的竞价观察算法，不能套用到普通策略。
SUPPORTED_PRESELECT_STRATEGY_IDS = frozenset(DEFAULT_PRESELECT_STRATEGY_IDS)
MAIN_BOARD_FILTER = ["沪主板", "深主板"]
DEFAULT_LIMIT_PER_STRATEGY = 5
MAX_LIMIT_PER_STRATEGY = 30
_CACHE_TTL_SECONDS = 60.0
_preselect_cache_lock = threading.Lock()
_preselect_cache: dict[tuple[Any, ...], tuple[float, dict]] = {}
logger = logging.getLogger(__name__)


def build_preselect_payload(
    repo,
    engine,
    *,
    as_of: date | None = None,
    trade_date: date | None = None,
    strategy_ids: list[str] | None = None,
    limit_per_strategy: int = DEFAULT_LIMIT_PER_STRATEGY,
    asset_type: str = "stock",
    timeframe: str = "1d",
) -> dict:
    """Build a read-only post-close preselect pool.

    The strict strategy result remains the final trading signal. This pool is a
    broader watchlist that can later be narrowed by auction/open snapshots.
    """
    now = datetime.now(tz=CN_TZ)
    svc = ScreenerService(repo, asset_type=asset_type)
    if as_of is not None:
        signal_date = as_of
    else:
        resolver = getattr(svc, "latest_strategy_date", None)
        signal_date = resolver() if resolver is not None else svc.latest_date()
    trade_day = trade_date or cn_today()
    requested_ids = _normalize_strategy_ids(strategy_ids)
    if requested_ids is None:
        requested_ids = [sid for sid in DEFAULT_PRESELECT_STRATEGY_IDS if engine.has(sid)]
    else:
        unknown = [sid for sid in requested_ids if not engine.has(sid)]
        if unknown:
            raise ValueError(f"unknown strategies: {unknown}")
        requested_ids = [
            sid for sid in requested_ids
            if sid in SUPPORTED_PRESELECT_STRATEGY_IDS
        ]
    limit = _limit(limit_per_strategy)

    base = {
        "mode": "preselect",
        "as_of": signal_date.isoformat() if signal_date else None,
        "trade_date": trade_day.isoformat(),
        "updated_at": _now_ms(now),
        "limit_per_strategy": limit,
        "results": {},
    }
    if asset_type != "stock":
        return {**base, "status": "unsupported_asset"}
    if signal_date is None:
        return {**base, "status": "no_history"}
    if not requested_ids:
        return {**base, "status": "empty_strategies"}

    unknown = [sid for sid in requested_ids if not engine.has(sid)]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}")

    data_dir = Path(repo.store.data_dir)
    overrides_map = strategy_config.list_overrides(data_dir)
    params_map = {
        sid: dict((overrides_map.get(sid) or {}).get("params") or {})
        for sid in requested_ids
    }
    requested_overrides = {
        sid: overrides_map.get(sid, {})
        for sid in requested_ids
    }
    cache_key = _cache_key(
        data_dir,
        asset_type,
        timeframe,
        signal_date,
        trade_day,
        requested_ids,
        limit,
        requested_overrides,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        context = svc.build_strategy_context(
            engine,
            signal_date,
            requested_ids,
            timeframe=timeframe,
            params_map=params_map,
            overrides_map=requested_overrides,
        )
    except ValueError as exc:
        return {**base, "status": "no_history", "error": str(exc)}

    frame = _target_frame(context.current, context.history, signal_date)
    results: dict[str, dict] = {}
    for sid in requested_ids:
        strategy = engine.get(sid)
        try:
            engine.validate_context(strategy, context)
        except ValueError:
            results[sid] = _empty_result(sid, signal_date, trade_day)
            continue
        params = engine.resolve_params(
            strategy,
            params_map.get(sid),
            requested_overrides.get(sid),
        )
        rows = _preselect_rows(
            frame,
            strategy_id=sid,
            strategy=strategy,
            params=params,
            overrides=requested_overrides.get(sid, {}),
            limit=limit,
        )
        results[sid] = {
            "strategy": sid,
            "as_of": signal_date.isoformat(),
            "trade_date": trade_day.isoformat(),
            "total": len(rows),
            "preselect_total": len(rows),
            "rows": rows,
        }

    payload = {**base, "status": "ready", "results": results}
    _cache_set(cache_key, payload)
    try:
        from app.services import strategy_history

        for sid, item in results.items():
            strategy = engine.get(sid)
            strategy_name = (strategy.meta.get("name") if strategy else None) or sid
            strategy_history.record_selection_snapshot(
                data_dir,
                strategy_id=sid,
                strategy_name=strategy_name,
                signal_date=signal_date.isoformat(),
                trade_date=trade_day.isoformat(),
                mode="preselect",
                rows=item.get("rows") or [],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("盘后预选历史记录失败: %s", exc)
    return payload


def _target_frame(
    current: pl.DataFrame | None,
    history: pl.DataFrame | None,
    signal_date: date,
) -> pl.DataFrame:
    parts = [
        df for df in (history, current)
        if df is not None and not df.is_empty()
    ]
    if not parts:
        return pl.DataFrame()
    panel = pl.concat(parts, how="diagonal_relaxed")
    if "symbol" not in panel.columns:
        return pl.DataFrame()
    if "date" not in panel.columns:
        return panel.unique(subset=["symbol"], keep="last")
    panel = (
        panel
        .filter(pl.col("date") <= signal_date)
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
    )
    if panel.is_empty():
        return panel
    exprs: list[pl.Expr] = []
    if "close" in panel.columns:
        exprs.append(pl.col("close").shift(1).over("symbol").alias("_pre_prev_close"))
        exprs.append(
            (
                pl.col("close") / pl.col("close").shift(9).over("symbol") - 1.0
            ).alias("_pre_cum9")
        )
    if "change_pct" in panel.columns:
        exprs.append(pl.col("change_pct").shift(1).over("symbol").alias("_pre_prev_change_pct"))
    if {"high", "close"}.issubset(panel.columns):
        exprs.append(
            (
                pl.col("high").shift(1).over("symbol")
                / pl.col("close").shift(2).over("symbol")
                - 1.0
            ).alias("_pre_prev_high_chg")
        )
    if exprs:
        panel = panel.with_columns(exprs)
    return panel.filter(pl.col("date") == signal_date).unique(subset=["symbol"], keep="last")


def _preselect_rows(
    frame: pl.DataFrame,
    *,
    strategy_id: str,
    strategy,
    params: dict,
    overrides: dict,
    limit: int,
) -> list[dict]:
    if frame.is_empty():
        return []
    required = {"symbol", "open", "high", "low", "close", "amount", "change_pct"}
    if not required.issubset(frame.columns):
        return []

    basic_filter = dict(strategy.basic_filter or {})
    if overrides.get("basic_filter"):
        basic_filter.update(overrides["basic_filter"])
    basic_filter["boards"] = MAIN_BOARD_FILTER
    df = StrategyEngine._apply_basic_filter(frame, basic_filter)
    if df.is_empty():
        return []

    # 跟随策略的临近严重异动剔除: 策略开了 avoid_abnormal 时, 预选池同样
    # 剔除信号日 9 日累计涨幅超限的票, 与正式信号/盘中提醒口径一致。
    if bool(params.get("avoid_abnormal")) and "_pre_cum9" in df.columns:
        cum9_max = float(params.get("abnormal_cum9_max", 60.0)) / 100.0
        df = df.filter(pl.col("_pre_cum9").is_null() | (pl.col("_pre_cum9") < cum9_max))
        if df.is_empty():
            return []

    primary = _apply_relaxed_dual_filter(df, params, strict=True)
    stage = "dual_relaxed"
    if primary.is_empty():
        primary = _apply_relaxed_dual_filter(df, params, strict=False)
        stage = "strength_watch"
    if primary.is_empty():
        return []

    scored = _with_preselect_score(primary, params, strategy_id, stage)
    scored = scored.sort("score", descending=True).head(limit)
    return _safe_rows(scored.to_dicts())


def _apply_relaxed_dual_filter(df: pl.DataFrame, params: dict, *, strict: bool) -> pl.DataFrame:
    close = pl.col("close")
    open_ = pl.col("open")
    high = pl.col("high")
    change = pl.col("change_pct")
    exprs: list[pl.Expr] = [
        close.is_not_null() & (close > 0),
        open_.is_not_null() & (open_ > 0),
        high.is_not_null() & (high > 0),
        pl.col("amount").is_not_null() & (pl.col("amount") > 0),
        close > open_,
        change.is_not_null() & (change >= (0.018 if strict else 0.003)),
        close >= high * (0.94 if strict else 0.90),
    ]
    if "ma20" in df.columns:
        exprs.append(close >= pl.col("ma20") * (0.96 if strict else 0.92))
    if "vol_ratio_5d" in df.columns:
        exprs.append(pl.col("vol_ratio_5d") >= (0.75 if strict else 0.45))
        exprs.append(pl.col("vol_ratio_5d") <= 8.0)
    if "annual_vol_20d" in df.columns:
        exprs.append(pl.col("annual_vol_20d") <= (1.10 if strict else 1.35))
    if "momentum_20d" in df.columns:
        exprs.append(pl.col("momentum_20d") > (-0.08 if strict else -0.18))
    return df.filter(pl.all_horizontal(exprs).fill_null(False))


def _with_preselect_score(
    df: pl.DataFrame,
    params: dict,
    strategy_id: str,
    stage: str,
) -> pl.DataFrame:
    close = pl.col("close")
    high = pl.col("high")
    low = pl.col("low")
    high_low_range = high - low
    close_pos = pl.when(high_low_range > 0).then((close - low) / high_low_range).otherwise(0.5)
    close_pos = _clip_expr(close_pos, 0.0, 1.0)
    change = pl.col("change_pct").fill_null(0.0)
    vol_ratio = _optional_col(df, "vol_ratio_5d", 1.0)
    momentum = _optional_col(df, "momentum_20d", 0.0)
    annual_vol = _optional_col(df, "annual_vol_20d", 0.8)
    amount_score = (
        pl.when(pl.col("amount") > 0)
        .then(pl.col("amount"))
        .otherwise(0.0)
        .log1p()
        * 0.06
    )
    ma20_premium = (
        pl.when(_optional_col(df, "ma20", 0.0) > 0)
        .then((close / _optional_col(df, "ma20", 1.0)) - 1.0)
        .otherwise(0.0)
    )
    prev_high_chg = _optional_col(df, "_pre_prev_high_chg", 0.0)
    prev_change = _optional_col(df, "_pre_prev_change_pct", 0.0)
    fenqi_prev_high = float(params.get("fenqi_prev_high", 7.5)) / 100.0
    fenqi_prev_close_max = float(params.get("fenqi_prev_close_max", 6.0)) / 100.0
    auction_min_vol = float(params.get("auction_min_vol", 1.55))
    branch = (
        pl.when(
            (prev_high_chg >= fenqi_prev_high * 0.65)
            & (prev_change <= fenqi_prev_close_max + 0.03)
        )
        .then(pl.lit("fenqi_watch"))
        .when((change >= 0.018) & (vol_ratio >= auction_min_vol * 0.45))
        .then(pl.lit("auction_watch"))
        .otherwise(pl.lit("trend_watch"))
    )
    score = (
        close_pos * 18.0
        + change * 180.0
        + _clip_expr(vol_ratio, 0.0, 5.0) * 4.0
        + momentum * 35.0
        + ma20_premium * 45.0
        + amount_score
        + prev_high_chg * 30.0
        - annual_vol * 5.0
    )
    return df.with_columns([
        score.alias("score"),
        branch.alias("preselect_branch"),
        pl.lit(stage).alias("preselect_stage"),
        pl.lit(strategy_id).alias("preselect_strategy"),
        pl.lit("watch_only").alias("preselect_status"),
    ])


def _optional_col(df: pl.DataFrame, name: str, default: float) -> pl.Expr:
    if name in df.columns:
        return pl.col(name).fill_null(default)
    return pl.lit(default)


def _clip_expr(expr: pl.Expr, lower: float, upper: float) -> pl.Expr:
    return pl.when(expr < lower).then(lower).when(expr > upper).then(upper).otherwise(expr)


def _empty_result(sid: str, signal_date: date, trade_day: date) -> dict:
    return {
        "strategy": sid,
        "as_of": signal_date.isoformat(),
        "trade_date": trade_day.isoformat(),
        "total": 0,
        "preselect_total": 0,
        "rows": [],
    }


def _normalize_strategy_ids(strategy_ids: list[str] | None) -> list[str] | None:
    if strategy_ids is None:
        return None
    out: list[str] = []
    for sid in strategy_ids:
        text = str(sid or "").strip()
        if text:
            out.append(text)
    return out


def _limit(value: int | None) -> int:
    try:
        parsed = int(value or DEFAULT_LIMIT_PER_STRATEGY)
    except (TypeError, ValueError):
        parsed = DEFAULT_LIMIT_PER_STRATEGY
    return max(1, min(parsed, MAX_LIMIT_PER_STRATEGY))


def _safe_rows(rows: list[dict]) -> list[dict]:
    safe: list[dict] = []
    for row in rows:
        item = {}
        for key, value in row.items():
            item[key] = _safe_value(value)
        safe.append(item)
    return safe


def _safe_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _now_ms(now: datetime) -> int:
    return int(now.timestamp() * 1000)


def _cache_key(
    data_dir: Path,
    asset_type: str,
    timeframe: str,
    signal_date: date,
    trade_day: date,
    strategy_ids: list[str],
    limit: int,
    overrides_map: dict[str, dict],
) -> tuple[Any, ...]:
    return (
        str(Path(data_dir).resolve()),
        asset_type,
        timeframe,
        signal_date.isoformat(),
        trade_day.isoformat(),
        tuple(strategy_ids),
        int(limit),
        _freeze_jsonish(overrides_map),
    )


def _cache_get(key: tuple[Any, ...]) -> dict | None:
    now = time.monotonic()
    with _preselect_cache_lock:
        entry = _preselect_cache.get(key)
        if entry is None:
            return None
        ts, payload = entry
        if now - ts <= _CACHE_TTL_SECONDS:
            return payload
        _preselect_cache.pop(key, None)
    return None


def _cache_set(key: tuple[Any, ...], payload: dict) -> None:
    now = time.monotonic()
    with _preselect_cache_lock:
        _preselect_cache[key] = (now, payload)
        if len(_preselect_cache) > 16:
            oldest = next(iter(_preselect_cache))
            _preselect_cache.pop(oldest, None)


def _freeze_jsonish(value):
    if isinstance(value, dict):
        return tuple(
            (str(key), _freeze_jsonish(val))
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_jsonish(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_jsonish(item) for item in value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)
