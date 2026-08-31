"""Screener API。"""
from __future__ import annotations

import glob as _glob
import logging
import math
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db_safe import is_valid_ext_ident, quote_ident
from app.services import strategy_cache
from app.services.auction_preselect import build_preselect_payload
from app.services.auction_confirmation import confirm_cached_strategy_results
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screener", tags=["screener"])


class CustomRequest(BaseModel):
    conditions: list[str]
    order_by: Optional[str] = None
    limit: int = 30
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"


class PresetRequest(BaseModel):
    strategy_id: str
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"
    timeframe: str = "1d"


class AuctionConfirmRequest(BaseModel):
    as_of: Optional[date] = None
    trade_date: Optional[date] = None
    strategy_ids: Optional[list[str]] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"


class PreselectRequest(BaseModel):
    as_of: date | None = None
    trade_date: date | None = None
    strategy_ids: list[str] | None = None
    limit_per_strategy: int = 5
    ext_columns: str | None = None
    asset_type: str = "stock"
    timeframe: str = "1d"


def _safe(result_dict: dict) -> dict:
    """sanitize for JSON(NaN / Inf → None)."""
    rows = result_dict.get("rows", [])
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return result_dict


def _one_word_limit_expr(status_main: str, columns: list[str]) -> Any:
    required = {"open", "high", "low", "close", "status"}
    if not required.issubset(columns):
        import polars as pl
        return pl.lit(False)

    import polars as pl
    return (
        (pl.col("status") == status_main)
        & (pl.col("close") > 0)
        & (pl.col("open") == pl.col("high"))
        & (pl.col("high") == pl.col("low"))
        & (pl.col("low") == pl.col("close"))
    ).fill_null(False)


def _safe_ext_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# 标识符安全原语 (转义 + 白名单) 集中在 app.db_safe, 见 Issue #150 注入防护。


# ── 扩展列 value_map 缓存 ────────────────────────────────────────────
# 每次请求 _load_ext_value_maps 都会重新从磁盘读 ext parquet 并重建 {symbol: value}。
# 用底层 parquet 文件的 (路径, mtime) 签名做 memoize: 文件未变则复用上次的 map,
# parquet 被重写 (mtime 变化) 时自动失效重算。仅缓存基于 config 的快照/时序路径,
# 无 config 的 DuckDB view 回退路径不缓存 (少见)。
_ext_value_map_cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}


def _ext_parquet_signature(cfg, data_dir) -> Optional[tuple]:
    """该扩展配置底层 parquet 文件的 (路径, mtime) 签名; 出错返回 None (禁用缓存)。"""
    try:
        from app.api.ext_data import _parquet_glob
        pattern = _parquet_glob(cfg, data_dir)
        files = sorted(_glob.glob(pattern, recursive=True))
        if not files:
            return None
        return tuple((f, os.path.getmtime(f)) for f in files)
    except Exception:  # noqa: BLE001
        return None


def _load_ext_value_maps(repo, ext_columns: Optional[str]) -> dict[str, dict[str, Any]]:
    """按请求加载扩展列，返回 {输出列名: {symbol: value}}。

    策略结果缓存是共享文件，不能被不同 ext_columns 组合污染；因此扩展列只在
    返回前通过该投影映射追加到结果副本中。

    基于 config 的路径按 parquet 文件 mtime 签名 memoize, 文件未变时跳过磁盘重读。
    """
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if not ext_specs:
        return {}

    import polars as pl

    from app.api.ext_data import _read_ext_dataframe
    from app.services.ext_data import ExtConfigStore

    db = repo.store.db
    data_dir = repo.store.data_dir
    ext_store = ExtConfigStore(data_dir)
    configs = {c.id: c for c in ext_store.load_all()}
    value_maps: dict[str, dict[str, Any]] = {}

    for config_id, field_name in ext_specs:
        out_col = f"{config_id}__{field_name}"
        cfg = configs.get(config_id)
        cache_key = (config_id, field_name)
        sig = _ext_parquet_signature(cfg, data_dir) if cfg else None
        try:
            if cfg:
                # 命中缓存 (文件签名一致) → 复用, 免去磁盘重读
                cached = _ext_value_map_cache.get(cache_key)
                if cached is not None and sig is not None and cached[0] == sig:
                    value_maps[out_col] = cached[1]
                    continue
                # 时序扩展表只取最新分区，避免历史分区把同一 symbol JOIN 放大。
                ext_df, _ = _read_ext_dataframe(cfg, data_dir)
            else:
                view_name = f"ext_{config_id}"
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                ).arrow())

            if ext_df.is_empty() or "symbol" not in ext_df.columns or field_name not in ext_df.columns:
                continue

            ext_df = ext_df.select(["symbol", field_name]).unique(subset=["symbol"], keep="last")
            vmap = {
                str(row["symbol"]): _safe_ext_value(row.get(field_name))
                for row in ext_df.to_dicts()
                if row.get("symbol")
            }
            value_maps[out_col] = vmap
            if cfg and sig is not None:
                _ext_value_map_cache[cache_key] = (sig, vmap)
        except Exception as e:  # noqa: BLE001
            logger.debug("screener ext column join skipped for %s.%s: %s", config_id, field_name, e)

    return value_maps


def _row_with_ext(row: dict, ext_values: dict[str, dict[str, Any]], symbol: Optional[str] = None) -> dict:
    next_row = dict(row)
    sym = symbol or next_row.get("symbol")
    for out_col, value_map in ext_values.items():
        next_row[out_col] = value_map.get(str(sym)) if sym else None
    return next_row


def _rows_with_ext(rows: list[dict], ext_values: dict[str, dict[str, Any]]) -> list[dict]:
    if not ext_values:
        return rows
    return [_row_with_ext(r, ext_values) for r in rows]


def _result_with_ext(result_dict: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return result_dict
    return {**result_dict, "rows": _rows_with_ext(result_dict.get("rows", []), ext_values)}


def _results_with_ext(results: dict[str, dict], ext_values: dict[str, dict[str, Any]]) -> dict[str, dict]:
    if not ext_values:
        return results
    return {sid: _result_with_ext(r, ext_values) for sid, r in results.items()}


def _cache_payload_with_ext(cached: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return cached

    payload = dict(cached)
    payload["results"] = _results_with_ext(cached.get("results", {}), ext_values)

    ever_rows = cached.get("today_ever_rows")
    if isinstance(ever_rows, dict):
        enriched_ever: dict[str, dict[str, dict]] = {}
        for sid, sym_map in ever_rows.items():
            if not isinstance(sym_map, dict):
                continue
            enriched_ever[sid] = {
                sym: _row_with_ext(row, ext_values, symbol=sym)
                for sym, row in sym_map.items()
                if isinstance(row, dict)
            }
        payload["today_ever_rows"] = enriched_ever

    return payload


def _update_cache_strategy(data_dir, as_of: str, strategy_id: str, safe_data: dict) -> None:
    """单跑后更新缓存中该策略的结果，保持缓存与最新计算一致。"""
    from app.services import strategy_cache
    cached = strategy_cache.read_cache(data_dir)
    if cached and cached.get("as_of") == as_of:
        results = cached.get("results", {})
        results[strategy_id] = {
            "total": safe_data.get("total", 0),
            "as_of": as_of,
            "rows": safe_data.get("rows", []),
        }
        strategy_cache.write_cache(data_dir, as_of, results)


@router.get("/strategies")
def strategies(
    request: Request,
    asset_type: str = Query("stock"),
    timeframe: str = Query("1d"),
):
    """兼容策略清单端点；唯一数据源为 StrategyEngine。"""
    data_dir = request.app.state.repo.store.data_dir
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")
    presets = []
    for meta in engine.list_strategies():
        if meta.get("research_only"):
            continue
        if asset_type not in meta.get("asset_types", ["stock"]):
            continue
        if timeframe not in meta.get("timeframes", ["1d"]):
            continue
        sid = meta["id"]
        overrides = strategy_config.load_override(data_dir, sid)
        presets.append({
            **meta,
            "name": overrides.get("name") or meta["name"],
            "description": overrides.get("description") or meta.get("description", ""),
        })

    return {"presets": presets, "load_errors": engine.load_errors()}


@router.post("/run")
def run_custom(req: CustomRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400,
                            detail="无可用数据日期 — enriched 表为空,请先运行盘后管道")
    result = svc.run(
        as_of=as_of,
        conditions=req.conditions,
        order_by=req.order_by,
        limit=req.limit,
        pool=req.pool,
    )
    safe_data = _safe(asdict(result))
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    return _result_with_ext(safe_data, ext_values)


@router.post("/run_preset")
def run_preset(req: PresetRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    # 加载用户保存的策略配置
    data_dir = request.app.state.repo.store.data_dir
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    overrides = strategy_config.load_override(data_dir, req.strategy_id)
    engine = getattr(request.app.state, "strategy_engine", None)
    if not engine:
        raise HTTPException(status_code=404, detail=f"策略引擎未初始化或策略 {req.strategy_id} 不存在")

    try:
        if not engine.has(req.strategy_id):
            raise ValueError(f"unknown strategy: {req.strategy_id}")
        if engine.get(req.strategy_id).meta.get("research_only"):
            raise ValueError(f"unknown strategy: {req.strategy_id}")
        params = dict(overrides.get("params") or {})
        context = svc.build_strategy_context(
            engine,
            as_of,
            [req.strategy_id],
            timeframe=req.timeframe,
            params_map={req.strategy_id: params},
            overrides_map={req.strategy_id: overrides or {}},
        )
        result = engine.run(
            req.strategy_id,
            context,
            pool=req.pool,
            params=params,
            overrides=overrides or None,
        )
    except ValueError as e:
        status_code = 404 if "unknown strategy" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e)) from e

    safe_data = _safe(asdict(result))
    # 分钟周期结果不写入盘后缓存 (strategy_cache 是日线语义, as_of/updated_at
    # 混入分钟结果会污染页面秒加载路径)。
    if req.timeframe == "1d":
        _update_cache_strategy(data_dir, str(as_of), req.strategy_id, safe_data)

    return _result_with_ext(safe_data, ext_values)


@router.post("/auction-confirmation")
def auction_confirmation(req: AuctionConfirmRequest, request: Request):
    """把盘后策略缓存和竞价 / 开盘快照拼成确认后的选股结果。"""
    data_dir = request.app.state.repo.store.data_dir
    cached = _cached_with_realtime(request)

    as_of = req.as_of
    if as_of is None:
        cached_as_of = cached.get("as_of")
        if cached_as_of:
            try:
                as_of = date.fromisoformat(str(cached_as_of))
            except ValueError:
                as_of = None

    confirmed = confirm_cached_strategy_results(
        data_dir,
        cached,
        as_of=as_of,
        trade_date=req.trade_date,
        strategy_ids=req.strategy_ids,
    )
    ext_values = _load_ext_value_maps(request.app.state.repo, req.ext_columns)
    results = {}
    for sid, payload in (confirmed.get("results") or {}).items():
        if not isinstance(payload, dict):
            continue
        rows = _rows_with_ext(payload.get("rows") or [], ext_values)
        results[sid] = {**payload, "rows": rows}

    confirmed = {**confirmed, "results": results}
    return confirmed


@router.post("/preselect")
def preselect(req: PreselectRequest, request: Request):
    """按盘后结果给出次交易日竞价前的主板预选池。"""
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")

    try:
        preselect_payload = build_preselect_payload(
            request.app.state.repo,
            engine,
            as_of=req.as_of,
            trade_date=req.trade_date,
            strategy_ids=req.strategy_ids,
            limit_per_strategy=req.limit_per_strategy,
            asset_type=req.asset_type,
            timeframe=req.timeframe,
        )
    except ValueError as exc:
        status_code = 404 if "unknown strategy" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    ext_values = _load_ext_value_maps(request.app.state.repo, req.ext_columns)
    results = {}
    for sid, item in (preselect_payload.get("results") or {}).items():
        if not isinstance(item, dict):
            continue
        rows = _rows_with_ext(item.get("rows") or [], ext_values)
        results[sid] = {**item, "rows": rows}

    return {**preselect_payload, "results": results}


def _cached_with_realtime(request: Request) -> dict:
    """读取盘后缓存，并用监控引擎的实时结果覆盖同策略。"""
    data_dir = request.app.state.repo.store.data_dir
    cached = strategy_cache.read_cache(data_dir)
    if cached is None:
        cached = {"as_of": None, "results": {}, "updated_at": None}

    # 叠加监控引擎内存里的实时结果 (若有), 用新鲜数据覆盖同策略的盘后结果
    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    if monitor_engine is not None:
        realtime_results = monitor_engine.latest_strategy_results()
        if realtime_results:
            results = dict(cached.get("results") or {})
            results.update(realtime_results)
            cached = dict(cached)
            cached["results"] = results
            # 有实时数据时, 以最新时间戳为准
            import time as _time
            cached["updated_at"] = int(_time.time() * 1000)

    return cached


@router.get("/cached")
def get_cached(
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """读取策略结果缓存, 并叠加监控引擎本轮实时算出的结果。

    - 盘后缓存 (strategy_cache.json): 非监控策略 / 页面秒加载用, run_all 写入。
    - 监控引擎内存结果 (latest_strategy_results): 实时行情每轮对「加入监控的策略」算出,
      不落盘 (避免与 read_cache 的 mtime 校验冲突), 在此直接叠加覆盖盘后结果。
      被监控的策略拿到新鲜数据, 非监控策略仍用盘后缓存。
    """
    cached = _cached_with_realtime(request)

    # 无任何数据 (盘后缓存空 + 无实时结果) → 返回空标记, 前端据此提示
    if not cached.get("results") and cached.get("as_of") is None:
        return {"as_of": None, "results": {}, "updated_at": None}

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    return _cache_payload_with_ext(cached, ext_values)


@router.get("/cached-summary")
def get_cached_summary(request: Request):
    """返回策略卡片所需的轻量摘要，不序列化股票明细。"""
    cached = _cached_with_realtime(request)
    results = cached.get("results") or {}
    summary = {
        sid: {
            "total": int(result.get("total") or 0),
            "as_of": result.get("as_of"),
        }
        for sid, result in results.items()
        if isinstance(result, dict)
    }

    cached_as_of = cached.get("as_of")
    ever_rows = cached.get("today_ever_rows") or {}
    ever_counts = {}
    for sid, result in results.items():
        if not isinstance(result, dict) or result.get("as_of") != cached_as_of:
            continue
        current_symbols = {
            str(row["symbol"])
            for row in result.get("rows") or []
            if isinstance(row, dict) and row.get("symbol")
        }
        ever_counts[sid] = len(set((ever_rows.get(sid) or {}).keys()) | current_symbols)
    return {
        "as_of": cached_as_of,
        "results": summary,
        "today_ever_counts": ever_counts,
        "updated_at": cached.get("updated_at"),
    }


@router.get("/cached-result/{strategy_id}")
def get_cached_result(
    strategy_id: str,
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """按需返回单个策略的完整明细及其今日失效行。"""
    cached = _cached_with_realtime(request)
    raw_result = (cached.get("results") or {}).get(strategy_id)
    if not isinstance(raw_result, dict):
        return {
            "result": None,
            "today_ever_rows": None,
            "strategy_ids_by_symbol": {},
            "updated_at": cached.get("updated_at"),
        }

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    result = {
        "as_of": raw_result.get("as_of"),
        "strategy": strategy_id,
        "rows": _rows_with_ext(raw_result.get("rows") or [], ext_values),
        "total": int(raw_result.get("total") or 0),
        "elapsed_ms": 0.0,
    }

    ever_rows = None
    if cached.get("as_of") == result["as_of"]:
        strategy_ever_rows = (cached.get("today_ever_rows") or {}).get(strategy_id)
        if isinstance(strategy_ever_rows, dict):
            ever_rows = {
                symbol: _row_with_ext(row, ext_values, symbol=symbol)
                for symbol, row in strategy_ever_rows.items()
                if isinstance(row, dict)
            }

    selected_symbols = {
        str(row["symbol"])
        for row in raw_result.get("rows") or []
        if isinstance(row, dict) and row.get("symbol")
    }
    strategy_ids_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in selected_symbols}
    for sid, cached_result in (cached.get("results") or {}).items():
        if not isinstance(cached_result, dict) or cached_result.get("as_of") != result["as_of"]:
            continue
        for row in cached_result.get("rows") or []:
            symbol = str(row.get("symbol")) if isinstance(row, dict) and row.get("symbol") else None
            if symbol in strategy_ids_by_symbol:
                strategy_ids_by_symbol[symbol].append(sid)

    return {
        "result": result,
        "today_ever_rows": ever_rows,
        "strategy_ids_by_symbol": strategy_ids_by_symbol,
        "updated_at": cached.get("updated_at"),
    }


_MARKET_SNAPSHOT_COLS = [
    "symbol", "name", "close", "change_pct", "amount", "volume",
    "turnover_rate", "vol_ratio_5d", "total_shares", "float_shares",
    "market_cap", "float_market_cap", "consecutive_limit_ups",
]


def _list_enriched_dates(data_dir, *, limit: int = 60) -> list[str]:
    """列出本地 kline_daily_enriched 可用交易日 (新→旧)。"""
    from pathlib import Path

    base = Path(data_dir) / "kline_daily_enriched"
    if not base.exists():
        return []
    dates: list[str] = []
    for p in base.glob("date=*"):
        if not p.is_dir():
            continue
        part = p / "part.parquet"
        if not part.exists():
            continue
        ds = p.name.split("=", 1)[-1]
        try:
            date.fromisoformat(ds)
        except ValueError:
            continue
        dates.append(ds)
    dates.sort(reverse=True)
    return dates[: max(1, int(limit))]


def _snapshot_rows_from_df(df):
    import polars as pl

    if df is None or df.is_empty():
        return []
    if "close" in df.columns and "total_shares" in df.columns:
        market_cap = pl.col("close") * pl.col("total_shares")
        if "market_cap" in df.columns:
            market_cap = pl.coalesce([market_cap, pl.col("market_cap")])
        df = df.with_columns(market_cap.alias("market_cap"))
    if "close" in df.columns and "float_shares" in df.columns:
        float_market_cap = pl.col("close") * pl.col("float_shares")
        if "float_market_cap" in df.columns:
            float_market_cap = pl.coalesce([float_market_cap, pl.col("float_market_cap")])
        df = df.with_columns(float_market_cap.alias("float_market_cap"))
    # 若缺 change_pct, 尽量从 close/prev_close 推
    if "change_pct" not in df.columns and "close" in df.columns and "prev_close" in df.columns:
        df = df.with_columns(
            (pl.col("close") / pl.col("prev_close") - 1).alias("change_pct")
        )
    cols = [c for c in _MARKET_SNAPSHOT_COLS if c in df.columns]
    rows = df.select(cols).to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return rows


def _overlay_snapshot_rows(base_df, overlay_rows: list[dict]):
    """用盘中 tick 子集覆盖全市场基础快照, 未命中标的保持基础快照。

    quote_ticks 在生产上可能是稀疏增量；直接把它当全市场快照会导致板块气泡清空。
    """
    import polars as pl

    if base_df is None or base_df.is_empty() or not overlay_rows:
        return base_df
    if "symbol" not in base_df.columns:
        return base_df
    try:
        overlay = pl.DataFrame(overlay_rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("snapshot overlay frame failed: %s", e)
        return base_df
    if overlay.is_empty() or "symbol" not in overlay.columns:
        return base_df

    overlay = overlay.unique(subset=["symbol"], keep="last")
    base_cols = set(base_df.columns)
    overlay_cols = [c for c in overlay.columns if c != "symbol"]
    joined = base_df.join(
        overlay.select(["symbol", *overlay_cols]),
        on="symbol",
        how="left",
        suffix="_tick",
    )
    exprs = []
    drop_cols: list[str] = []
    for col in overlay_cols:
        if col not in base_cols:
            continue
        tick_col = f"{col}_tick"
        if tick_col not in joined.columns:
            continue
        exprs.append(pl.coalesce([pl.col(tick_col), pl.col(col)]).alias(col))
        drop_cols.append(tick_col)
    if exprs:
        joined = joined.with_columns(exprs)
    if drop_cols:
        joined = joined.drop(drop_cols)
    return joined


def _intraday_snapshot_from_quote_ticks(
    data_dir,
    trade_date: date,
    *,
    as_of_ts: int | None = None,
):
    """从 quote_ticks 取截止 as_of_ts 的每标的最后一笔, 构造成 market-snapshot 行。

    用于盘中/收盘后回放「今天已过时间点」。无 quote_ticks 时返回空。
    snapshot_as_of 会同时读取热缓存和磁盘分区, 不能先按目录存在性短路。
    """
    from pathlib import Path

    import polars as pl

    from app.services import quote_tick_store

    try:
        rows, actual_ts = quote_tick_store.snapshot_as_of(
            Path(data_dir),
            target_date=trade_date,
            as_of_ts=as_of_ts,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("intraday snapshot read failed: %s", e)
        return [], None

    if not rows:
        return [], None

    try:
        df = pl.DataFrame(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("intraday snapshot frame failed: %s", e)
        return [], None

    if df.is_empty() or "symbol" not in df.columns or "event_ts" not in df.columns:
        return [], None

    close_col = "last_price" if "last_price" in df.columns else ("close" if "close" in df.columns else None)
    exprs = [pl.col("symbol")]
    if close_col:
        exprs.append(pl.col(close_col).alias("close"))
    if "change_pct" in df.columns:
        exprs.append(pl.col("change_pct"))
    elif close_col and "prev_close" in df.columns:
        exprs.append((pl.col(close_col) / pl.col("prev_close") - 1).alias("change_pct"))
    for c in ("name", "amount", "volume", "prev_close", "open", "high", "low"):
        if c in df.columns:
            exprs.append(pl.col(c))
    out = df.select(exprs)

    try:
        inst_path = Path(data_dir) / "instruments" / "instruments.parquet"
        if inst_path.exists():
            inst = pl.read_parquet(inst_path)
            inst_cols = [c for c in ("symbol", "name", "total_shares", "float_shares") if c in inst.columns]
            if "symbol" in inst_cols:
                joined = out.join(
                    inst.select(inst_cols).unique(subset=["symbol"], keep="last"),
                    on="symbol",
                    how="left",
                )
                if "name" in out.columns and "name_right" in joined.columns:
                    joined = joined.with_columns(
                        pl.coalesce([pl.col("name"), pl.col("name_right")]).alias("name")
                    ).drop("name_right")
                elif "name" not in out.columns and "name" in joined.columns:
                    pass
                out = joined
    except Exception as e:  # noqa: BLE001
        logger.debug("intraday snapshot join instruments failed: %s", e)

    return _snapshot_rows_from_df(out), actual_ts


def _market_replay_min_symbols(data_dir) -> int:
    """估算全市场回放至少应覆盖的股票数；无维表时不启用覆盖度门槛。"""
    from pathlib import Path

    import polars as pl

    inst_path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0
    try:
        inst = pl.read_parquet(inst_path, columns=["symbol"])
    except Exception as e:  # noqa: BLE001
        logger.debug("market replay instrument count skipped: %s", e)
        return 0
    count = inst["symbol"].drop_nulls().n_unique() if "symbol" in inst.columns else 0
    if count <= 0:
        return 0
    return max(20, min(1000, int(count * 0.5)))


def _timeline_needs_backfill(timeline: dict, data_dir, target: date) -> bool:
    if not timeline.get("has_ticks"):
        return True
    min_symbols = _market_replay_min_symbols(data_dir)
    if min_symbols <= 0:
        return False
    return int(timeline.get("symbol_count") or 0) < min_symbols


def _cn_today_safe() -> date:
    try:
        from app.market_time import cn_today

        return cn_today()
    except Exception:  # noqa: BLE001
        return date.today()


def _try_fetch_today_quotes(request: Request, *, reason: str) -> bool:
    quote_service = getattr(request.app.state, "quote_service", None)
    if quote_service is None:
        return False
    try:
        quote_service._fetch_quotes()  # noqa: SLF001
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("%s realtime quote fetch failed: %s", reason, e)
        return False


def _is_realtime_collection_target(target: date) -> bool:
    if target != _cn_today_safe():
        return False
    try:
        from app.market_time import trading_minutes_elapsed

        return trading_minutes_elapsed() < 240
    except Exception:  # noqa: BLE001
        now = datetime.now()
        return now.hour < 15 or (now.hour == 15 and now.minute < 1)


def _enqueue_quote_tick_backfill(
    request: Request,
    data_dir,
    target: date,
    *,
    reason: str,
    force: bool = False,
    min_symbols: int | None = None,
) -> dict:
    from pathlib import Path

    from app.services.quote_tick_backfill import quote_tick_backfill_service

    return quote_tick_backfill_service.enqueue(
        Path(data_dir),
        target,
        repo=getattr(request.app.state, "repo", None),
        reason=reason,
        force=force,
        min_symbols=min_symbols,
    )


def _ensure_market_replay_ticks(
    request: Request,
    data_dir,
    target: date,
    *,
    step_seconds: int = 60,
) -> dict:
    """确保目标日有板块回放帧；历史缺失时触发补数据。"""
    from pathlib import Path

    from app.services import quote_tick_store

    data_path = Path(data_dir)
    timeline = quote_tick_store.timeline_points(
        data_path,
        target_date=target,
        step_seconds=step_seconds,
    )
    min_symbols = _market_replay_min_symbols(data_path)
    if _is_realtime_collection_target(target):
        needs_backfill = _timeline_needs_backfill(timeline, data_path, target)
        if needs_backfill and _try_fetch_today_quotes(request, reason="market replay"):
            timeline = quote_tick_store.timeline_points(
                data_path,
                target_date=target,
                step_seconds=step_seconds,
            )
            needs_backfill = _timeline_needs_backfill(timeline, data_path, target)
        queued = None
        if needs_backfill:
            queued = _enqueue_quote_tick_backfill(
                request,
                data_path,
                target,
                reason="today_sparse_quote_ticks" if timeline.get("has_ticks") else "today_missing_quote_ticks",
                force=True,
                min_symbols=min_symbols,
            )
        return {
            "status": (
                "ready"
                if not needs_backfill
                else "partial_ticks"
                if timeline.get("has_ticks")
                else "missing_today_ticks"
            ),
            "timeline": timeline,
            "backfill": queued,
        }

    if not _timeline_needs_backfill(timeline, data_path, target):
        return {"status": "ready", "timeline": timeline, "backfill": None}

    backfill = quote_tick_store.materialize_from_minute(data_path, target_date=target)
    if backfill.get("status") in {"materialized", "exists"}:
        timeline = quote_tick_store.timeline_points(
            data_path,
            target_date=target,
            step_seconds=step_seconds,
        )
        if not _timeline_needs_backfill(timeline, data_path, target):
            return {"status": backfill.get("status"), "timeline": timeline, "backfill": backfill}

    queued = _enqueue_quote_tick_backfill(
        request,
        data_path,
        target,
        reason="timeline_sparse_quote_ticks" if timeline.get("has_ticks") else "timeline_missing_quote_ticks",
        force=True,
        min_symbols=min_symbols,
    )

    if timeline.get("has_ticks"):
        return {"status": "partial_ticks", "timeline": timeline, "backfill": queued}
    return {"status": queued.get("status") or backfill.get("status") or "missing_ticks", "timeline": timeline, "backfill": queued}


@router.get("/market-dates")
def market_dates(request: Request, limit: int = Query(60, ge=1, le=250)):
    """返回本地可用的行情快照交易日列表 (新→旧), 供板块动能页日期选择。"""
    repo = request.app.state.repo
    dates = _list_enriched_dates(repo.store.data_dir, limit=limit)
    latest = dates[0] if dates else None
    return {"dates": dates, "latest": latest, "count": len(dates)}


@router.get("/market-snapshot")
def market_snapshot(
    request: Request,
    as_of: Optional[str] = Query(None, description="交易日 YYYY-MM-DD; 默认最新 enriched 日"),
    as_of_ts: Optional[int] = Query(
        None,
        description="盘中/回放截止时间 (ms epoch)。当目标日期存在 quote_ticks 时生效",
    ),
):
    """全市场轻量行情快照，供板块/概念聚合与动能气泡使用。

    - 默认: 最新 enriched 日收盘快照
    - as_of=历史日: 该日 enriched 收盘快照
    - as_of + as_of_ts: 优先从目标日 quote_ticks 回放到该时刻; 无 ticks 则退回日快照
    """
    import polars as pl

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    data_dir = repo.store.data_dir

    # 解析目标日
    target: date | None = None
    if as_of:
        try:
            target = date.fromisoformat(str(as_of)[:10])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid as_of: {as_of}") from e
    else:
        target = svc.latest_date()
    if not target:
        return {
            "as_of": None,
            "as_of_ts": None,
            "mode": "empty",
            "rows": [],
            "available_dates": _list_enriched_dates(data_dir, limit=60),
        }

    mode = "eod"
    actual_ts: int | None = None
    rows: list[dict] = []
    intraday_rows: list[dict] = []
    replay_status: str | None = None
    replay: dict | None = None

    # 盘中/历史回放: 只要目标日有 quote_ticks, 就按 as_of_ts 取该时点最后一笔。
    if as_of_ts is not None:
        replay = _ensure_market_replay_ticks(request, data_dir, target)
        replay_status = str(replay.get("status") or "")
        intraday_rows, actual_ts = _intraday_snapshot_from_quote_ticks(
            data_dir, target, as_of_ts=int(as_of_ts),
        )
        if intraday_rows:
            mode = "intraday_partial" if replay_status == "partial_ticks" else "intraday"
        else:
            # 无 ticks → 退回日快照
            mode = "eod_fallback"

    df = svc._load_enriched_for_date(target)
    # 若 enriched 缺 change_pct, 用前一日 close 补
    if not df.is_empty() and "change_pct" not in df.columns and "close" in df.columns:
        try:
            # 找前一可用 enriched 日
            dates = _list_enriched_dates(data_dir, limit=30)
            prev = None
            for ds in dates:
                d = date.fromisoformat(ds)
                if d < target:
                    prev = d
                    break
            if prev is not None:
                prev_path = data_dir / "kline_daily" / f"date={prev.isoformat()}" / "part.parquet"
                if prev_path.exists():
                    prev_df = pl.read_parquet(prev_path).select(
                        ["symbol", pl.col("close").alias("prev_close")]
                    )
                    df = df.join(prev_df, on="symbol", how="left").with_columns(
                        (pl.col("close") / pl.col("prev_close") - 1).alias("change_pct")
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug("market_snapshot prev_close fill failed: %s", e)
    # JOIN name if missing
    if not df.is_empty() and "name" not in df.columns:
        try:
            inst = repo.get_instruments_asset("stock")
            if inst is not None and not inst.is_empty():
                cols = [c for c in ("symbol", "name", "total_shares", "float_shares") if c in inst.columns]
                df = df.join(inst.select(cols).unique(subset=["symbol"], keep="last"), on="symbol", how="left")
        except Exception:  # noqa: BLE001
            pass
    if intraday_rows:
        df = _overlay_snapshot_rows(df, intraday_rows)
    rows = _snapshot_rows_from_df(df)
    if not rows and intraday_rows:
        rows = intraday_rows
    if mode == "eod" and as_of_ts is None:
        mode = "eod"

    return {
        "as_of": str(target),
        "as_of_ts": actual_ts if mode.startswith("intraday") else as_of_ts,
        "mode": mode,
        "replay_status": replay_status,
        "rows": rows,
        "count": len(rows),
        "available_dates": _list_enriched_dates(data_dir, limit=60),
        "backfill": replay.get("backfill") if replay is not None else None,
    }


@router.get("/market-intraday-timeline")
def market_intraday_timeline(
    request: Request,
    as_of: Optional[str] = Query(None, description="交易日 YYYY-MM-DD, 默认今天"),
    step_seconds: int = Query(60, ge=30, le=600, description="时间轴采样步长(秒)"),
):
    """返回某日 quote_ticks 可用的回放时间点列表 (ms)。

    用于动能页拖动条: 盘中显示「已过时间」, 收盘后可全日回放。
    无 quote_ticks 时返回空 points。
    """
    repo = request.app.state.repo
    data_dir = repo.store.data_dir
    today = _cn_today_safe()

    if as_of:
        try:
            target = date.fromisoformat(str(as_of)[:10])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid as_of: {as_of}") from e
    else:
        target = today

    try:
        replay = _ensure_market_replay_ticks(request, data_dir, target, step_seconds=step_seconds)
        timeline = replay.get("timeline") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("timeline replay ensure failed: %s", e)
        return {
            "as_of": str(target),
            "points": [],
            "start_ts": None,
            "end_ts": None,
            "has_ticks": False,
            "error": str(e),
        }

    if not timeline.get("has_ticks"):
        backfill = replay.get("backfill") if isinstance(replay, dict) else None
        return {
            "as_of": str(target),
            "points": [],
            "start_ts": None,
            "end_ts": None,
            "has_ticks": False,
            "step_seconds": step_seconds,
            "backfill_status": replay.get("status") if isinstance(replay, dict) else "missing_ticks",
            "backfill": backfill,
            "message": "该日期没有盘中 tick，也没有本地分钟K可补" if (backfill or {}).get("status") == "missing_minute" else "该日期暂无盘中回放点",
        }

    return {
        "as_of": str(target),
        "points": timeline.get("points") or [],
        "start_ts": timeline.get("start_ts"),
        "end_ts": timeline.get("end_ts"),
        "step_seconds": step_seconds,
        "has_ticks": True,
        "count": timeline.get("count") or len(timeline.get("points") or []),
        "symbol_count": timeline.get("symbol_count") or 0,
        "sources": timeline.get("sources") or [],
        "backfill_status": replay.get("status"),
        "backfill": replay.get("backfill"),
    }


@router.post("/run_all")
def run_all(request: Request, body: Optional[dict] = None):
    """批量运行指定策略；注册、路由和执行均由 StrategyEngine 负责。"""
    from datetime import date as date_type

    t_total = time.perf_counter()

    body = body or {}
    repo = request.app.state.repo
    asset_type = str(body.get("asset_type") or "stock")
    timeframe = str(body.get("timeframe") or "1d")
    svc = ScreenerService(repo, asset_type=asset_type)
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")

    # 解析日期
    raw_date = body.get("as_of")
    if raw_date:
        as_of = date_type.fromisoformat(str(raw_date)) if isinstance(raw_date, str) else raw_date
    else:
        as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "results": {}}

    data_dir = request.app.state.repo.store.data_dir

    requested_ids = body.get("strategy_ids")
    if requested_ids and isinstance(requested_ids, list):
        all_ids = [str(sid) for sid in requested_ids]
        unknown = [
            sid
            for sid in all_ids
            if not engine.has(sid) or engine.get(sid).meta.get("research_only")
        ]
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown strategies: {unknown}")
    else:
        all_ids = [
            meta["id"]
            for meta in engine.list_strategies()
            if not meta.get("research_only")
            and asset_type in meta.get("asset_types", ["stock"])
            and timeframe in meta.get("timeframes", ["1d"])
        ]

    if not all_ids:
        return {"as_of": str(as_of), "results": {}}

    # 批量预加载所有 override 配置
    t0 = time.perf_counter()
    all_overrides = strategy_config.list_overrides(data_dir)
    logger.info("run_all: list_overrides took %.1fms (%d overrides)", (time.perf_counter() - t0) * 1000, len(all_overrides))

    params_map = {
        sid: dict((all_overrides.get(sid) or {}).get("params") or {})
        for sid in all_ids
    }
    overrides_map = {sid: all_overrides.get(sid, {}) for sid in all_ids}
    try:
        context = svc.build_strategy_context(
            engine,
            as_of,
            all_ids,
            timeframe=timeframe,
            params_map=params_map,
            overrides_map=overrides_map,
        )
        engine_results = engine.run_all(
            context,
            params_map=params_map,
            overrides_map=overrides_map,
            strategy_ids=all_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    results: dict[str, dict] = {}
    for sid, result in engine_results.items():
        safe_rows = _safe(asdict(result)).get("rows", [])
        results[sid] = {
            "total": result.total,
            "as_of": str(as_of),
            "rows": safe_rows,
        }

    elapsed = (time.perf_counter() - t_total) * 1000
    logger.info("run_all: total took %.1fms (%d strategies)", elapsed, len(all_ids))

    # 写入策略缓存 (供页面秒加载); 分钟周期结果不落盘 (日线语义缓存)
    if results and timeframe == "1d":
        try:
            strategy_cache.write_cache(data_dir, str(as_of), results)
        except Exception:  # noqa: BLE001
            pass

    if body.get("summary_only"):
        return {
            "as_of": str(as_of),
            "results": {
                sid: {"total": result["total"], "as_of": result["as_of"]}
                for sid, result in results.items()
            },
        }

    ext_values = _load_ext_value_maps(repo, body.get("ext_columns"))
    return {"as_of": str(as_of), "results": _results_with_ext(results, ext_values)}


@router.get("/limit-ladder")
def limit_ladder(
    request: Request,
    as_of: Optional[date] = None,
    direction: str = Query("up", description="up=涨停梯队 | down=跌停梯队"),
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """连板/连跌梯队 — 按连板数分组, 含三状态。
    返回: tiers = [{ boards, count, stocks: [{symbol,name,change_pct,status,...}] }]

    direction=up (默认):
      status: limit_up=涨停 | broken=炸板(摸板未封) | failed=断板(晋级失败)
    direction=down:
      status: limit_down=跌停 | recovery=翘板(跌停后回升,含收阳条件) | failed=止跌(昨日跌停今日未跌停也未翘板)

    ext_columns: 动态 JOIN 扩展数据, 如 "concept.concept,industry.industry"
    """
    import polars as pl

    is_down = direction == "down"

    # 按 direction 参数化字段映射
    if is_down:
        sig_col = "signal_limit_down"
        consec_col = "consecutive_limit_downs"
        broken_col = "signal_limit_down_recovery"
        status_main, status_broken, status_failed = "limit_down", "recovery", "failed"
    else:
        sig_col = "signal_limit_up"
        consec_col = "consecutive_limit_ups"
        broken_col = "signal_broken_limit_up"
        status_main, status_broken, status_failed = "limit_up", "broken", "failed"

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    as_of = as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "tiers": [], "counts": {"up": 0, "down": 0}}

    # 双方向涨跌停计数(不论当前 direction, 前端始终同时显示)
    limit_up_mask = (
        pl.col("signal_limit_up").fill_null(False)
        if "signal_limit_up" in df.columns else pl.lit(False)
    )
    limit_down_mask = (
        pl.col("signal_limit_down").fill_null(False)
        if "signal_limit_down" in df.columns else pl.lit(False)
    )
    limit_up_symbols = set(
        df.filter(limit_up_mask)["symbol"].to_list()
    ) if "signal_limit_up" in df.columns and "symbol" in df.columns else set()
    limit_down_symbols = set(
        df.filter(limit_down_mask)["symbol"].to_list()
    ) if "signal_limit_down" in df.columns and "symbol" in df.columns else set()
    count_up_raw = len(limit_up_symbols)
    count_down_raw = len(limit_down_symbols)

    # 双方向 sealed 修正: 只扣“当前仍判定为涨/跌停”集合内的假封板。
    # 旧 depth5 可能是残缺名单(或隔日残留), 若对整表 fake 直接相减会把计数扣穿
    # (典型: raw 95/315 被扣成 69/259, 而 fake 几乎都不在当前涨跌停集合里)。
    depth_svc_global = getattr(request.app.state, "depth_service", None)
    fake_up = 0
    fake_down = 0
    sealed_up_ready = False
    sealed_down_ready = False
    up_map: dict = {}
    down_map: dict = {}
    if depth_svc_global:
        up_map = depth_svc_global.get_sealed_map(as_of, is_down=False) or {}
        down_map = depth_svc_global.get_sealed_map(as_of, is_down=True) or {}
        sealed_up_ready = bool(up_map) and depth_svc_global.is_sealed_ready(as_of)
        sealed_down_ready = bool(down_map) and depth_svc_global.is_sealed_ready(as_of)
        if up_map and limit_up_symbols:
            fake_up = sum(
                1 for sym, v in up_map.items()
                if sym in limit_up_symbols and v.get("sealed") is False
            )
        if down_map and limit_down_symbols:
            fake_down = sum(
                1 for sym, v in down_map.items()
                if sym in limit_down_symbols and v.get("sealed") is False
            )
    count_up = max(0, count_up_raw - fake_up) if sealed_up_ready else count_up_raw
    count_down = max(0, count_down_raw - fake_down) if sealed_down_ready else count_down_raw

    # 双方向 sealed 明细(供前端弹窗同时显示涨跌停)
    def _count_sealed(m: dict, ready: bool, active_symbols: set[str] | None = None):
        if not m or not ready:
            return {"real": 0, "fake": 0, "pending": 0}
        items = (
            ((sym, v) for sym, v in m.items() if sym in active_symbols)
            if active_symbols is not None else m.items()
        )
        real = fake = pending = 0
        for _, v in items:
            sealed = v.get("sealed")
            if sealed is True:
                real += 1
            elif sealed is False:
                fake += 1
            else:
                pending += 1
        return {"real": real, "fake": fake, "pending": pending}
    sealed_counts_up = _count_sealed(up_map, sealed_up_ready, limit_up_symbols)
    sealed_counts_down = _count_sealed(down_map, sealed_down_ready, limit_down_symbols)

    # 加载前一日的 prev consecutive_limit_ups/downs
    # 窄读: 仅取前一交易日的 [symbol, consec_col] 两列 (存储列, 直接谓词下推读 parquet),
    # 替代旧的 range(1,10) 循环逐日 _load_enriched_for_date 全量指标重算 (最坏 9× 全市场重算)。
    prev_consec: pl.DataFrame = svc.load_prior_consecutive(as_of, consec_col)

    if not prev_consec.is_empty():
        df = df.join(prev_consec, on="symbol", how="left")
    else:
        df = df.with_columns(pl.lit(0).cast(pl.UInt32).alias("prev_consec"))

    # 表达式
    is_limit = pl.col(sig_col).fill_null(False) if sig_col in df.columns else pl.lit(False)
    is_broken = pl.col(broken_col).fill_null(False) if broken_col in df.columns else pl.lit(False)
    consec = pl.col(consec_col).fill_null(0) if consec_col in df.columns else pl.lit(0)
    prev_c = pl.col("prev_consec").fill_null(0)

    # 计算 status + boards (结构涨跌停对称, 仅字段与字面量不同)
    is_failed = ~is_limit & ~is_broken & (prev_c > 0)
    df = df.with_columns([
        pl.when(is_limit).then(pl.lit(status_main))
        .when(is_broken).then(pl.lit(status_broken))
        .when(is_failed).then(pl.lit(status_failed))
        .otherwise(None).alias("status"),
        pl.when(is_limit).then(consec)
        .when(is_broken | is_failed).then(prev_c + 1)
        .otherwise(0).cast(pl.UInt32).alias("boards"),
    ])

    df = df.filter(pl.col("status").is_not_null() & (pl.col("boards") > 0))

    # ── 五档 sealed 叠加(独立旁路, 不改 signal_limit_up) ──
    # 假涨停(收盘价=涨停价但卖一有量)从 limit 降级为 broken(归炸板视图)
    # 真涨停保留 + 附封单量; sealed=null(待确认/降级)保持原状
    depth_svc = getattr(request.app.state, "depth_service", None)
    sealed_ready = False
    sealed_age: float | None = None
    if depth_svc:
        # 复用上方双方向计数已读取的 sealed map: 同一请求、同一 as_of、同一对象,
        # 不再第三次读取 (内存路径含全量浅拷贝, parquet 路径含整文件读)。
        sealed_map = down_map if is_down else up_map
        sealed_ready = bool(sealed_map) and depth_svc.is_sealed_ready(as_of)
        sealed_age = depth_svc.get_sealed_age(as_of) if sealed_ready else None

        if sealed_map:
            # 构建 sealed 列(symbol → sealed bool, vol)
            sym_sealed = {s: v.get("sealed") for s, v in sealed_map.items()}
            sym_vol = {s: v.get("vol") for s, v in sealed_map.items()}

            # JOIN sealed: 对每只 status=main 的票, 看 sealed 值
            sealed_rows = pl.DataFrame({
                "symbol": list(sym_sealed.keys()),
                "_sealed": list(sym_sealed.values()),
                "_sealed_vol": list(sym_vol.values()),
            }) if sym_sealed else pl.DataFrame()

            if not sealed_rows.is_empty():
                df = df.join(sealed_rows, on="symbol", how="left")
                # 假涨停(main 状态但 sealed=False)→ 降级为 broken
                df = df.with_columns(
                    pl.when(
                        (pl.col("status") == status_main)
                        & pl.col("_sealed").is_not_null()
                        & (pl.col("_sealed") == False)  # noqa: E712
                    ).then(pl.lit(status_broken))
                    .otherwise(pl.col("status")).alias("status"),
                    # sealed_status: real/fake/pending/null
                    pl.when(
                        (pl.col("status") == status_main)
                        & (pl.col("_sealed") == True)  # noqa: E712
                    ).then(pl.lit("real"))
                    .when(
                        (pl.col("_sealed") == False)  # noqa: E712
                    ).then(pl.lit("fake"))
                    .when(
                        (pl.col("status") == status_main)
                        & pl.col("_sealed").is_null()
                    ).then(pl.lit("pending"))
                    .otherwise(None).alias("sealed_status"),
                    pl.col("_sealed_vol").alias("sealed_vol"),
                ).drop(["_sealed", "_sealed_vol"])
            else:
                df = df.with_columns(
                    pl.lit(None).alias("sealed_status"),
                    pl.lit(None).alias("sealed_vol"),
                )
        else:
            df = df.with_columns(
                pl.lit(None).alias("sealed_status"),
                pl.lit(None).alias("sealed_vol"),
            )
    else:
        df = df.with_columns(
            pl.lit(None).alias("sealed_status"),
            pl.lit(None).alias("sealed_vol"),
        )

    df = df.with_columns(_one_word_limit_expr(status_main, df.columns).alias("is_one_word"))

    # 动态 JOIN 扩展数据
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    ext_col_names: list[str] = []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = ext_df.rename({field_name: ext_col_name})
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
                    ext_col_names.append(ext_col_name)
            except Exception:
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        from app.api.ext_data import _parquet_glob
                        glob = _parquet_glob(cfg, data_dir)
                        ext_df = pl.read_parquet(glob)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = ext_df.select(["symbol", field_name]).rename({field_name: ext_col_name})
                            df = df.join(ext_df, on="symbol", how="left")
                            ext_col_names.append(ext_col_name)
                    except Exception:
                        pass

    # 选择输出列
    cols = ["symbol", "name", "close", "change_pct", "boards", "status", consec_col, "sealed_status", "sealed_vol", "is_one_word"] + ext_col_names
    df = df.select([c for c in cols if c in df.columns])
    # 排序: boards 降序, status 按主状态→炸/翘→断/止
    status_order = pl.when(pl.col("status") == status_main).then(0)
    status_order = status_order.when(pl.col("status") == status_broken).then(1)
    status_order = status_order.otherwise(2).alias("_status_order")
    df = df.with_columns(status_order).sort(["boards", "_status_order"], descending=[True, False]).drop("_status_order")

    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    # 按 boards 分组
    tiers: dict[int, list] = {}
    for r in rows:
        n = int(r.get("boards") or 0)
        tiers.setdefault(n, []).append(r)

    tier_list = [
        {"boards": n, "count": len(stocks), "stocks": stocks}
        for n, stocks in sorted(tiers.items(), key=lambda x: -x[0])
    ]

    return {
        "as_of": str(as_of),
        "tiers": tier_list,
        "counts": {"up": count_up, "down": count_down},
        "counts_raw": {"up": count_up_raw, "down": count_down_raw},
        "sealed_ready": sealed_ready,
        "sealed_age": round(sealed_age, 0) if sealed_age is not None else None,
        "sealed_counts": {
            "real": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "real"),
            "fake": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "fake"),
            "pending": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "pending"),
        },
        "sealed_counts_up": sealed_counts_up,
        "sealed_counts_down": sealed_counts_down,
    }


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]。"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if not config_id or not field_name:
            continue
        if not is_valid_ext_ident(config_id) or "\x00" in field_name:
            continue
        result.append((config_id, field_name))
    return result
