"""股权溢价指数(股债溢价)计算。

口径(A 股常见「股债溢价」):
    ERP(%) = 全市场市值加权盈利收益率(%) − 10 年期国债收益率(%)
    盈利收益率 = 1 / PE × 100
    PE = Σ(市值) / Σ(归母净利润)  (仅计入净利润 > 0 的样本)

数据源:
    - 股价 / 市值: 调用方传入的当日行情 close + 股本
    - 净利润: data/financials/metrics (最近一期报告)
    - 10Y 国债: 东方财富 RPTA_WEB_TREASURYYIELD (EMM00166466), 失败时回退缓存

牛熊分档(绝对值阈值, 适合主页一眼读):
    ≥ 4.0  极度低估 · 牛
    ≥ 3.0  低估 · 偏牛
    ≥ 2.0  中性 · 震荡
    ≥ 1.0  高估 · 偏熊
    <  1.0  极度高估 · 熊
"""
from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import polars as pl

logger = logging.getLogger(__name__)

# 东方财富 中债国债收益率:10年
_BOND_10Y_FIELD = "EMM00166466"
_BOND_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
    "?sortColumns=SOLAR_DATE&sortTypes=-1&pageSize=1&pageNumber=1"
    "&reportName=RPTA_WEB_TREASURYYIELD&columns=ALL&quoteColumns="
)
_BOND_TTL_S = 3600.0  # 国债日频, 1h 缓存足够
_BOND_TIMEOUT_S = 6.0

_bond_lock = threading.Lock()
_bond_cache: dict[str, Any] | None = None
_bond_cache_ts: float = 0.0

# 财务 metrics 读盘缓存: 文件 mtime 未变则复用, 避免每次 overview 都扫 parquet
_metrics_lock = threading.Lock()
_metrics_cache: pl.DataFrame | None = None
_metrics_mtime: float | None = None
_metrics_path: Path | None = None


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def classify_regime(erp: float) -> dict[str, Any]:
    """根据股权溢价绝对值分档, 返回 label / tone / hint。"""
    if erp >= 4.0:
        return {
            "label": "极度低估 · 牛",
            "tone": "bull",
            "hint": "股票相对国债显著便宜, 历史偏牛配置区",
        }
    if erp >= 3.0:
        return {
            "label": "低估 · 偏牛",
            "tone": "lean_bull",
            "hint": "股票性价比偏高, 中长期配置价值较好",
        }
    if erp >= 2.0:
        return {
            "label": "中性 · 震荡",
            "tone": "neutral",
            "hint": "股债性价比大致均衡, 方向性不强",
        }
    if erp >= 1.0:
        return {
            "label": "高估 · 偏熊",
            "tone": "lean_bear",
            "hint": "股票相对国债偏贵, 进攻性价比下降",
        }
    return {
        "label": "极度高估 · 熊",
        "tone": "bear",
        "hint": "盈利收益率接近/低于国债, 历史偏熊/拥挤区",
    }


def fetch_bond_yield_10y(*, force: bool = False) -> dict[str, Any] | None:
    """拉取最新 10 年期国债收益率(%), 带进程内缓存。

    返回 {"yield": float, "as_of": "YYYY-MM-DD", "source": "eastmoney"} 或 None。
    """
    global _bond_cache, _bond_cache_ts
    now = time.time()
    with _bond_lock:
        if (
            not force
            and _bond_cache is not None
            and (now - _bond_cache_ts) < _BOND_TTL_S
        ):
            return dict(_bond_cache)

    try:
        resp = httpx.get(
            _BOND_URL,
            timeout=_BOND_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = (payload.get("result") or {}).get("data") or []
        if not rows:
            raise ValueError("empty bond yield payload")
        row = rows[0]
        y = _finite(row.get(_BOND_10Y_FIELD))
        if y is None:
            raise ValueError(f"missing field {_BOND_10Y_FIELD}")
        solar = str(row.get("SOLAR_DATE") or "")
        as_of = solar[:10] if solar else None
        result = {"yield": round(y, 4), "as_of": as_of, "source": "eastmoney"}
        with _bond_lock:
            _bond_cache = result
            _bond_cache_ts = time.time()
        return dict(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch 10Y bond yield failed: %s", exc)
        with _bond_lock:
            # 失败时若有旧缓存, 继续用(哪怕过期)
            if _bond_cache is not None:
                stale = dict(_bond_cache)
                stale["stale"] = True
                return stale
        return None


def _load_metrics_profit(data_dir: Path) -> pl.DataFrame:
    """读取 metrics 中的 (symbol, net_profit, total_shares), mtime 缓存。"""
    global _metrics_cache, _metrics_mtime, _metrics_path
    path = data_dir / "financials" / "metrics" / "part.parquet"
    if not path.exists():
        # 兼容 hive 分片
        candidates = sorted((data_dir / "financials" / "metrics").rglob("*.parquet"))
        if not candidates:
            return pl.DataFrame()
        path = candidates[0]

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return pl.DataFrame()

    with _metrics_lock:
        if (
            _metrics_cache is not None
            and _metrics_path == path
            and _metrics_mtime == mtime
        ):
            return _metrics_cache

    try:
        df = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read financials metrics failed: %s", exc)
        return pl.DataFrame()

    cols = [c for c in ("symbol", "net_profit", "total_shares") if c in df.columns]
    if "symbol" not in cols or "net_profit" not in cols:
        return pl.DataFrame()
    out = df.select(cols).unique(subset=["symbol"], keep="last")

    with _metrics_lock:
        _metrics_cache = out
        _metrics_mtime = mtime
        _metrics_path = path
    return out


def _load_instrument_shares(data_dir: Path) -> pl.DataFrame:
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        df = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return pl.DataFrame()
    cols = [c for c in ("symbol", "total_shares") if c in df.columns]
    if len(cols) < 2:
        return pl.DataFrame()
    return df.select(cols).rename({"total_shares": "inst_shares"})


def compute_market_pe(
    rows: list[dict],
    data_dir: Path,
) -> dict[str, Any] | None:
    """用行情 rows + 财务/股本 计算市值加权 PE。

    rows 至少需要 symbol, close; 可选 total_shares。
    """
    if not rows:
        return None

    price_rows = []
    for r in rows:
        symbol = str(r.get("symbol") or "").strip().upper()
        close = _finite(r.get("close"))
        if not symbol or close is None or close <= 0:
            continue
        shares = _finite(r.get("total_shares"))
        price_rows.append({"symbol": symbol, "close": close, "row_shares": shares})
    if not price_rows:
        return None

    # 同一 symbol 可能因 enriched 分片重复出现, 取最后一条避免重复加权
    prices = pl.DataFrame(price_rows).unique(subset=["symbol"], keep="last")
    metrics = _load_metrics_profit(data_dir)
    if metrics.is_empty():
        return None
    inst = _load_instrument_shares(data_dir)

    joined = prices.join(metrics, on="symbol", how="inner")
    if not inst.is_empty():
        joined = joined.join(inst, on="symbol", how="left")
    else:
        joined = joined.with_columns(pl.lit(None).cast(pl.Float64).alias("inst_shares"))

    share_exprs = []
    if "total_shares" in joined.columns:
        share_exprs.append(pl.col("total_shares"))
    share_exprs.append(pl.col("row_shares"))
    share_exprs.append(pl.col("inst_shares"))

    joined = joined.with_columns(pl.coalesce(share_exprs).alias("shares")).filter(
        (pl.col("net_profit") > 0)
        & (pl.col("close") > 0)
        & (pl.col("shares").is_not_null())
        & (pl.col("shares") > 0)
    )
    if joined.is_empty():
        return None

    joined = joined.with_columns((pl.col("close") * pl.col("shares")).alias("mcap"))
    total_mcap = float(joined["mcap"].sum())
    total_profit = float(joined["net_profit"].sum())
    if total_profit <= 0 or total_mcap <= 0:
        return None
    pe = total_mcap / total_profit
    if not math.isfinite(pe) or pe <= 0:
        return None
    earnings_yield = 100.0 / pe
    return {
        "pe": round(pe, 2),
        "earnings_yield": round(earnings_yield, 4),
        "sample_count": int(joined.height),
        "total_mcap": total_mcap,
        "total_profit": total_profit,
    }


def build_equity_premium(
    rows: list[dict],
    data_dir: Path | str,
    *,
    price_as_of: str | None = None,
    price_source: str = "eod",
    quote_age_ms: float | None = None,
) -> dict[str, Any]:
    """装配股权溢价指数 payload(始终返回 dict, 失败时 value=None)。

    时效说明(必须对前端诚实):
      - price_source=live: 盘中全市场最新价覆盖后的市值
      - price_source=eod:  最新交易日收盘价(非 tick 实时)
      - 国债: 东方财富日频, 盘后才更新; 失败回退缓存时 stale_bond=True
      - 盈利: 本地 financials/metrics 最近一期报告, 不是实时
    """
    data_dir = Path(data_dir)
    empty = {
        "value": None,
        "pe": None,
        "earnings_yield": None,
        "bond_yield_10y": None,
        "bond_as_of": None,
        "sample_count": 0,
        "label": "暂无",
        "tone": "neutral",
        "hint": "缺少估值或国债数据, 暂无法计算股权溢价",
        "formula": "1/PE − 10Y国债收益率",
        "price_as_of": price_as_of,
        "price_source": price_source,
        "quote_age_ms": quote_age_ms,
        "freshness": "unavailable",
        "quality_warning": None,
    }

    pe_info = compute_market_pe(rows, data_dir)
    bond = fetch_bond_yield_10y()
    if not pe_info or not bond:
        missing = []
        if not pe_info:
            missing.append("市场PE")
        if not bond:
            missing.append("国债收益率")
        empty["hint"] = f"缺少{'/'.join(missing)}, 暂无法计算股权溢价"
        if pe_info:
            empty["pe"] = pe_info["pe"]
            empty["earnings_yield"] = pe_info["earnings_yield"]
            empty["sample_count"] = pe_info["sample_count"]
        if bond:
            empty["bond_yield_10y"] = bond["yield"]
            empty["bond_as_of"] = bond.get("as_of")
            empty["stale_bond"] = bool(bond.get("stale"))
        return empty

    erp = pe_info["earnings_yield"] - float(bond["yield"])
    regime = classify_regime(erp)
    pe = pe_info["pe"]
    # 全 A 市值加权 PE 历史常见约 10–25; 过低/过高多半是财务口径异常, 必须提示
    quality_warning = None
    if pe < 8:
        quality_warning = (
            f"全市场PE={pe:.1f} 显著低于历史常态(约10–25), "
            "可能是财务净利润口径/报告期异常, 牛熊分档仅供参考勿直接当作绝对估值"
        )
    elif pe > 50:
        quality_warning = (
            f"全市场PE={pe:.1f} 显著高于历史常态(约10–25), "
            "可能是财务净利润口径/报告期异常, 牛熊分档仅供参考"
        )

    if price_source == "live":
        freshness = "live"
    elif bond.get("stale"):
        freshness = "stale"
    else:
        freshness = "eod"

    return {
        "value": round(erp, 2),
        "pe": pe,
        "earnings_yield": round(pe_info["earnings_yield"], 2),
        "bond_yield_10y": round(float(bond["yield"]), 2),
        "bond_as_of": bond.get("as_of"),
        "sample_count": pe_info["sample_count"],
        "label": regime["label"],
        "tone": regime["tone"],
        "hint": regime["hint"],
        "formula": "1/PE − 10Y国债收益率",
        "stale_bond": bool(bond.get("stale")),
        "price_as_of": price_as_of,
        "price_source": price_source,
        "quote_age_ms": quote_age_ms,
        "freshness": freshness,
        "quality_warning": quality_warning,
    }
