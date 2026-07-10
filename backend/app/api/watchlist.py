"""自选股 API。"""
from __future__ import annotations

import logging
import time
from datetime import date, time as dt_time

import polars as pl
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.market_time import cn_now, cn_today
from app.services import watchlist
from app.services.symbols import normalize_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddRequest(BaseModel):
    symbol: str
    note: str = ""


class BatchAddRequest(BaseModel):
    symbols: list[str]
    note: str = ""


def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        # 股票 + ETF 名称统一由 repo.get_name_map 解析, 自选列表可混合持有
        name_by_symbol = request.app.state.repo.get_name_map([r.get("symbol") for r in rows])
        if not name_by_symbol:
            return rows
        return [{**row, "name": name_by_symbol.get(row.get("symbol"))} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("attach watchlist names failed: %s", e)
        return rows


@router.get("")
def list_all(request: Request):
    return {"symbols": _with_names(watchlist.list_symbols(), request)}


@router.post("")
def add_one(req: AddRequest, request: Request):
    symbol = normalize_symbol(req.symbol, request.app.state.repo)
    rows = watchlist.add(symbol, req.note)
    return {"symbols": _with_names(rows, request)}


@router.post("/batch")
def add_batch(req: BatchAddRequest, request: Request):
    repo = request.app.state.repo
    for sym in req.symbols:
        watchlist.add(normalize_symbol(sym, repo), req.note)
    return {"symbols": _with_names(watchlist.list_symbols(), request), "added": len(req.symbols)}


@router.post("/{symbol}/top")
def move_one_to_top(symbol: str, request: Request):
    symbol = normalize_symbol(symbol, request.app.state.repo)
    rows = watchlist.move_to_top(symbol)
    return {"symbols": _with_names(rows, request)}


@router.delete("/{symbol}")
def remove_one(symbol: str, request: Request):
    symbol = normalize_symbol(symbol, request.app.state.repo)
    rows = watchlist.remove(symbol)
    return {"symbols": _with_names(rows, request)}


@router.delete("")
def clear_all():
    """清空自选列表。"""
    count = watchlist.clear()
    return {"removed": count}


# 自选页需要的列
_WATCHLIST_COLS = [
    "symbol", "close", "change_pct", "change_amount", "amount",
    "turnover_rate",
    "amplitude", "annual_vol_20d",
    "vol_ratio_5d", "realtime_vol_ratio",
    "ma5", "ma10", "ma20", "ma60",
    "vol_ma5", "vol_ma10",
    "high_60d", "low_60d",
    "rsi_6", "rsi_14", "rsi_24",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
    "atr_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "consecutive_limit_ups", "consecutive_limit_downs",
    "signal_limit_up", "signal_limit_down", "signal_volume_surge",
    "signal_ma_golden_5_20", "signal_macd_golden", "signal_n_day_high",
    "signal_boll_breakout_upper", "signal_ma20_breakout",
    "signal_ma_dead_5_20", "signal_macd_dead", "signal_n_day_low",
    "signal_boll_breakdown_lower", "signal_ma20_breakdown",
]


@router.get("/enriched")
def watchlist_enriched(
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """自选股 enriched 数据 — 直接从 enriched 最新日读取, 无即时计算。

    ext_columns 参数示例: "industry_rating.score,fund_flow.net_inflow"
    会动态 LEFT JOIN 对应的 ext_{config_id} DuckDB view。
    """
    t0 = time.perf_counter()

    repo = request.app.state.repo
    symbols = [r["symbol"] for r in watchlist.list_symbols()]
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0}

    # 按资产拆分自选 symbol; ETF enriched 是独立缓存, 仅自选真的含 ETF 才去加载
    # (避免无 ETF 用户在缓存冷启动时触发 ETF 全量懒加载)
    etf_set = repo.get_etf_symbol_set()
    stock_symbols = [s for s in symbols if s not in etf_set]
    etf_symbols = [s for s in symbols if s in etf_set]

    df_e, cache_date = repo.get_enriched_latest()

    # 以自选列表为主表 LEFT JOIN enriched, 保证自选的每一只都返回一行;
    # 不在 enriched 缓存里的标的 (新股/冷门股/新用户未同步) 指标为 null, 前端渲染为 "—".
    # 旧实现是 df_e.filter(is_in(stock_symbols)), 方向反了 (以 enriched 为主),
    # 会把不在缓存 universe 里的自选股静默丢弃.
    if stock_symbols:
        watchlist_df = pl.DataFrame({"symbol": stock_symbols})
        if df_e.is_empty():
            df = watchlist_df
        else:
            df = watchlist_df.join(df_e, on="symbol", how="left")
    else:
        df = pl.DataFrame()

    # ETF 行合并; 缺失列 (换手率/涨跌停信号等) 为 null
    etf_date = None
    if etf_symbols:
        df_etf_all, etf_date = repo.get_enriched_latest_asset("etf")
        etf_watchlist_df = pl.DataFrame({"symbol": etf_symbols})
        if not df_etf_all.is_empty():
            # ETF 同样以自选为主表 LEFT JOIN, 缺失标的指标为 null
            df_etf = etf_watchlist_df.join(df_etf_all, on="symbol", how="left")
        else:
            df_etf = etf_watchlist_df
        df = df_etf if df.is_empty() else pl.concat([df, df_etf], how="diagonal_relaxed")

    # as_of 取两类缓存中较旧者, 避免把旧的 ETF 行标成股票缓存日期
    dates = [d for d in (cache_date if stock_symbols else None, etf_date) if d is not None]
    as_of = min(dates) if dates else None
    if df.is_empty():
        return {"rows": [], "as_of": str(as_of) if as_of else None, "elapsed_ms": 0}

    # JOIN float_shares (仅股票有) + 名称 (股票/ETF 统一走 get_name_map)
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "float_shares" in df_i.columns:
        df = df.join(df_i.select(["symbol", "float_shares"]), on="symbol", how="left")
    name_map = repo.get_name_map(df["symbol"].to_list())
    df = df.with_columns(
        pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name")
    )
    df = _attach_realtime_vol_ratio(df, repo, stock_symbols, as_of)

    # 选择内置需要的列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares"] if c in df.columns]
    df = df.select(keep)

    # 动态 JOIN 扩展数据表
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore
        from app.api.ext_data import _read_ext_dataframe

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                # 扩展时序数据必须只取最新分区；否则一个 symbol 会按历史分区数被 JOIN 放大。
                cfg = configs.get(config_id)
                if cfg:
                    ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                else:
                    ext_df = pl.from_arrow(db.query(
                        f"SELECT symbol, \"{field_name}\" FROM {view_name}"
                    ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = (
                        ext_df
                        .select(["symbol", field_name])
                        .unique(subset=["symbol"], keep="last")
                        .rename({field_name: ext_col_name})
                    )
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
            except Exception:
                # view 不存在或字段不存在，尝试直接读 parquet
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = (
                                ext_df
                                .select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug("ext join fallback failed for %s.%s: %s", config_id, field_name, e2)

    # sanitize NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    # 按自选添加顺序（新加的在前）重排行
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(pl.col("symbol").map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32).alias("_sort_order"))
    df = df.sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(as_of) if as_of else None, "elapsed_ms": elapsed}


def _attach_realtime_vol_ratio(
    df: pl.DataFrame,
    repo,
    symbols: list[str],
    as_of: date | str | None,
) -> pl.DataFrame:
    """给自选页补盘中量比展示值，不改变策略使用的 vol_ratio_5d 口径。"""
    if df.is_empty() or "volume" not in df.columns or "symbol" not in df.columns:
        return df

    as_of_date = _coerce_date(as_of)
    elapsed = _trading_day_elapsed_fraction(as_of_date)
    if as_of_date is None or elapsed is None or elapsed <= 0:
        return _ensure_realtime_vol_ratio_null(df)

    avg_by_symbol = _load_prev5_volume_avg(repo, symbols, as_of_date)
    if not avg_by_symbol:
        return _ensure_realtime_vol_ratio_null(df)

    avg_df = pl.DataFrame({
        "symbol": list(avg_by_symbol.keys()),
        "_prev5_vol_avg": list(avg_by_symbol.values()),
    })
    df = df.join(avg_df, on="symbol", how="left")
    df = df.with_columns(
        pl.when(
            pl.col("volume").is_not_null()
            & pl.col("_prev5_vol_avg").is_not_null()
            & (pl.col("_prev5_vol_avg") > 0)
        )
        .then(pl.col("volume").cast(pl.Float64) / (pl.col("_prev5_vol_avg") * elapsed))
        .otherwise(None)
        .alias("realtime_vol_ratio")
    )
    return df.drop("_prev5_vol_avg")


def _ensure_realtime_vol_ratio_null(df: pl.DataFrame) -> pl.DataFrame:
    if "realtime_vol_ratio" in df.columns:
        return df
    return df.with_columns(pl.lit(None).cast(pl.Float64).alias("realtime_vol_ratio"))


def _load_prev5_volume_avg(repo, symbols: list[str], as_of: date) -> dict[str, float]:
    if not symbols or not hasattr(repo, "execute_all"):
        return {}
    unique_symbols = [s for s in dict.fromkeys(symbols) if s]
    if not unique_symbols:
        return {}

    placeholders = ",".join("?" for _ in unique_symbols)
    try:
        rows = repo.execute_all(
            f"""
            WITH ranked AS (
                SELECT
                    symbol,
                    CAST(volume AS DOUBLE) AS volume,
                    row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM kline_daily
                WHERE symbol IN ({placeholders}) AND date < ?
            )
            SELECT symbol, avg(volume) AS avg_volume
            FROM ranked
            WHERE rn <= 5
            GROUP BY symbol
            """,
            [*unique_symbols, as_of],
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("load prev5 volume avg failed: %s", e)
        return {}
    return {str(symbol): float(avg) for symbol, avg in rows if avg not in (None, 0)}


def _trading_day_elapsed_fraction(as_of: date | None) -> float | None:
    """A 股当日已交易进度；非当天按完整交易日处理。"""
    if as_of is None:
        return None
    if as_of != cn_today():
        return 1.0

    t = cn_now().time()
    minutes = _minutes_since_open(t)
    if minutes <= 0:
        return None
    return min(minutes / 240.0, 1.0)


def _minutes_since_open(t: dt_time) -> int:
    if t < dt_time(9, 30):
        return 0
    if t < dt_time(11, 30):
        return (t.hour - 9) * 60 + t.minute - 30
    if t < dt_time(13, 0):
        return 120
    if t < dt_time(15, 0):
        return 120 + (t.hour - 13) * 60 + t.minute
    return 240


def _coerce_date(value: date | str | None) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if config_id and field_name:
            result.append((config_id, field_name))
    return result
