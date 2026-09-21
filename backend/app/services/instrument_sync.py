"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据, flatten ext 字段, 写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。

涨跌停价 (limit_up/down) 在入库前必须用本地昨收重算校验:
  部分数据源会回填过期或“下一交易日”口径的 limit, 直接入库会污染涨停统计。
"""
from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]
_INSTRUMENT_META_FIELDS = [
    "listing_date",
    "total_shares",
    "float_shares",
    "tick_size",
    "limit_up",
    "limit_down",
]
# 与 indicators.pipeline 保持一致: 主板 ST 2026-07-06 起 10%。
_ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE = date(2026, 7, 6)
# 与 compute_limit_signals 一致: 超过 1 分钱视为脏维表价。
_LIMIT_PRICE_TOLERANCE = 0.011
_LIMIT_SENTINEL = 10000.0
_SHARE_FIELDS = ("total_shares", "float_shares")
_NON_EQUITY_CODE_PREFIXES = ("07", "08")


class InstrumentSyncError(RuntimeError):
    """维表同步结果不安全, 拒绝覆盖现有主数据。"""


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up")
        row["limit_down"] = ext.get("limit_down")
        rows.append(row)
    return rows


def _fetch_instruments_via_provider() -> list[dict] | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(仍应走 tickflow)时返回 None。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def _fetch_instruments_via_tickflow() -> list[dict]:
    """走 TickFlow 免费 instruments 元数据接口拉全量股票维表。"""
    tf = get_client()
    all_rows: list[dict] = []
    for ex in _EXCHANGES:
        try:
            items = tf.exchanges.get_instruments(ex, instrument_type="stock")
            if items:
                rows = _flatten_instruments(items)
                all_rows.extend(rows)
                logger.info("instruments %s: %d stocks", ex, len(rows))
        except Exception as e:
            logger.warning("get_instruments(%s) failed: %s", ex, e)
    return all_rows


def _rows_by_symbol(rows: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in rows or []:
        symbol = str(raw.get("symbol") or "").strip().upper()
        if symbol:
            out[symbol] = dict(raw)
    return out


def _merge_instrument_rows(
    primary_rows: list[dict],
    fallback_rows: list[dict],
    existing_rows: list[dict] | None = None,
) -> list[dict]:
    """合并标的元数据, 空值不得覆盖已有有效值。

    行情 provider 是当前标的集合的主权威; TickFlow 和本地 instruments 只
    补充 provider 没有的字段, 且本地旧行不能把已经退市的标的重新带回结果。
    """
    primary = _rows_by_symbol(primary_rows)
    fallback = _rows_by_symbol(fallback_rows)
    existing = _rows_by_symbol(existing_rows)
    symbols = list(dict.fromkeys([*fallback.keys(), *primary.keys()]))
    merged: list[dict] = []

    for symbol in symbols:
        base = dict(existing.get(symbol, {}))
        # fallback 只能填空, 不能覆盖本地已经确认的股本; primary 的非空值
        # 才能更新当前基础元数据。两类来源都不能用 None 清空旧值。
        for field, value in (fallback.get(symbol) or {}).items():
            if value not in (None, "") and base.get(field) in (None, ""):
                base[field] = value
        for field, value in (primary.get(symbol) or {}).items():
            if value not in (None, "") or field not in base:
                base[field] = value
        base["symbol"] = symbol
        merged.append(base)
    return merged


def _finite_share(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def _as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _load_existing_instrument_rows(data_dir: Path) -> list[dict]:
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return []
    try:
        return pl.read_parquet(path).to_dicts()
    except Exception as e:
        logger.warning("读取旧 instruments 失败, 将不使用旧值补全: %s", e)
        return []


def _load_latest_share_map(data_dir: Path, as_of: date) -> dict[str, dict[str, float]]:
    """按公告日点时读取最新股本, 只返回 as_of 当日已经可用的数据。"""
    path = data_dir / "financials" / "shares" / "part.parquet"
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path)
    except Exception as e:
        logger.warning("读取 financials/shares 失败: %s", e)
        return {}
    if "symbol" not in df.columns or not any(field in df.columns for field in _SHARE_FIELDS):
        return {}

    prepared: list[tuple[str, date, str, dict[str, float]]] = []
    for raw in df.to_dicts():
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        available = _as_date(raw.get("announce_date")) or _as_date(raw.get("period_end"))
        if available is None or available > as_of:
            continue
        values: dict[str, float] = {}
        for field in _SHARE_FIELDS:
            value = _finite_share(raw.get(field))
            if value is not None:
                values[field] = value
        if values:
            prepared.append((symbol, available, str(raw.get("period_end") or ""), values))

    prepared.sort(key=lambda item: (item[0], item[1], item[2]))
    out: dict[str, dict[str, float]] = {}
    for symbol, _available, _period_end, values in prepared:
        out.setdefault(symbol, {}).update(values)
    return out


def _is_non_equity_symbol(symbol: str) -> bool:
    code = str(symbol or "").split(".", 1)[0]
    return code.startswith(_NON_EQUITY_CODE_PREFIXES)


def _latest_daily_symbols(data_dir: Path, as_of: date) -> set[str]:
    """取最近一个已落盘日 K 分区的股票集合, 作为当前可交易宇宙。"""
    root = data_dir / "kline_daily"
    if not root.exists():
        return set()
    dates: list[date] = []
    for path in root.glob("date=*"):
        if not path.is_dir():
            continue
        day = _as_date(path.name[5:])
        if day is not None and day <= as_of:
            dates.append(day)
    if not dates:
        return set()
    part = root / f"date={max(dates).isoformat()}" / "part.parquet"
    try:
        return {
            str(symbol).strip().upper()
            for symbol in pl.read_parquet(part, columns=["symbol"])["symbol"].drop_nulls().to_list()
            if str(symbol).strip()
        }
    except Exception as e:
        logger.warning("读取最近日 K 标的集合失败: %s", e)
        return set()


def _share_coverage(
    rows: list[dict],
    *,
    active_symbols: set[str] | None = None,
) -> tuple[int, int, list[str]]:
    eligible = [
        row for row in rows
        if not _is_non_equity_symbol(row.get("symbol", ""))
        and (not active_symbols or str(row.get("symbol", "")).upper() in active_symbols)
    ]
    valid = [
        row for row in eligible
        if _finite_share(row.get("total_shares")) is not None
        and _finite_share(row.get("float_shares")) is not None
    ]
    missing = [
        str(row.get("symbol"))
        for row in eligible
        if _finite_share(row.get("total_shares")) is None
        or _finite_share(row.get("float_shares")) is None
    ]
    return len(eligible), len(valid), missing


def _limit_pct_for_symbol(symbol: str, name: str | None, as_of: date) -> float:
    """板块 + ST 规则下的涨跌幅限制。"""
    code = str(symbol or "").split(".", 1)[0]
    is_chinext = code.startswith(("300", "301"))
    is_star = code.startswith(("688", "689"))
    is_bj = str(symbol or "").upper().endswith(".BJ") or code.startswith(("8", "4", "9"))
    if is_chinext or is_star:
        board = 0.20
    elif is_bj:
        board = 0.30
    else:
        board = 0.10

    is_st = "ST" in str(name or "").upper()
    if (
        is_st
        and not (is_chinext or is_star or is_bj)
        and as_of < _ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE
    ):
        return 0.05
    return board


def _limit_price_value(prev: float, limit_pct: float, *, up: bool) -> float:
    """与 pipeline._limit_price 相同的分整数算术, 返回 float 元。"""
    sign = 1 if up else -1
    num = round((1 + sign * limit_pct) * 100)  # 105/95, 110/90, ...
    cents = int(prev * 100 + 0.5)
    return ((cents * num + 50) // 100) / 100.0


def _finite_price(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    if price >= _LIMIT_SENTINEL:
        return None
    return price


def _latest_prev_close_map(data_dir: Path, as_of: date) -> tuple[date | None, dict[str, float]]:
    """读取本地日 K, 取 as_of 之前最近一个交易日的收盘价作涨跌停基准。"""
    root = data_dir / "kline_daily"
    if not root.exists():
        return None, {}

    dates: list[date] = []
    for path in root.glob("date=*"):
        if not path.is_dir():
            continue
        try:
            day = date.fromisoformat(path.name[5:])
        except ValueError:
            continue
        dates.append(day)
    if not dates:
        return None, {}

    dates.sort()
    earlier = [d for d in dates if d < as_of]
    base_day = earlier[-1] if earlier else dates[-1]
    part = root / f"date={base_day.isoformat()}" / "part.parquet"
    if not part.exists():
        return base_day, {}
    try:
        df = pl.read_parquet(part, columns=["symbol", "close"])
    except Exception as e:
        logger.warning("读取昨收基准失败(%s): %s", part, e)
        return base_day, {}

    out: dict[str, float] = {}
    for row in df.iter_rows(named=True):
        symbol = str(row.get("symbol") or "").strip().upper()
        close = _finite_price(row.get("close"))
        if symbol and close is not None:
            out[symbol] = close
    return base_day, out


def sanitize_limit_prices(
    rows: list[dict],
    *,
    prev_close_by_symbol: dict[str, float],
    as_of: date,
    base_date: date | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """校验/重算 limit_up/down, 返回新行列表与统计。

    规则:
      1. 有昨收: 用板块规则算理论涨跌停价
      2. 上游价与理论价差 ≤ 1 分: 保留上游 (source=provider)
      3. 否则改写为理论价 (source=theoretical)
      4. 无昨收: 清空 limit, 避免脏值入库
    """
    stats = {
        "total": 0,
        "kept_provider": 0,
        "rewritten": 0,
        "cleared": 0,
        "no_prev_close": 0,
    }
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        stats["total"] += 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            out.append(row)
            continue
        row["symbol"] = symbol
        prev = prev_close_by_symbol.get(symbol)
        if prev is None:
            row["limit_up"] = None
            row["limit_down"] = None
            row["limit_base_date"] = None
            row["limit_source"] = "missing_prev_close"
            stats["cleared"] += 1
            stats["no_prev_close"] += 1
            out.append(row)
            continue

        pct = _limit_pct_for_symbol(symbol, row.get("name"), as_of)
        theo_up = _limit_price_value(prev, pct, up=True)
        theo_down = _limit_price_value(prev, pct, up=False)
        src_up = _finite_price(row.get("limit_up"))
        src_down = _finite_price(row.get("limit_down"))

        up_ok = src_up is not None and abs(src_up - theo_up) <= _LIMIT_PRICE_TOLERANCE
        down_ok = src_down is not None and abs(src_down - theo_down) <= _LIMIT_PRICE_TOLERANCE
        if up_ok and down_ok:
            row["limit_up"] = round(src_up, 2)
            row["limit_down"] = round(src_down, 2)
            row["limit_source"] = "provider"
            stats["kept_provider"] += 1
        else:
            row["limit_up"] = theo_up
            row["limit_down"] = theo_down
            row["limit_source"] = "theoretical"
            stats["rewritten"] += 1
        row["limit_base_date"] = base_date.isoformat() if base_date else None
        out.append(row)
    return out, stats


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。
    """
    provider_rows = _fetch_instruments_via_provider()
    if provider_rows is None:
        all_rows = _fetch_instruments_via_tickflow()
        fallback_rows: list[dict] = []
    else:
        all_rows = provider_rows
        try:
            # TickFlow 仍可能提供 provider 缺失的 listing/share 元数据;
            # 失败只降级, 不能阻塞 tdxapi 基础列表同步。
            fallback_rows = _fetch_instruments_via_tickflow()
        except Exception as e:
            logger.warning("TickFlow instruments fallback failed: %s", e)
            fallback_rows = []

    if not all_rows:
        return 0

    as_of = cn_today()
    existing_rows = _load_existing_instrument_rows(data_dir)
    share_map = _load_latest_share_map(data_dir, as_of)
    share_rows = [
        {"symbol": symbol, **values}
        for symbol, values in share_map.items()
    ]
    # 先把本地旧维表和财务历史合成一个可靠 fallback, 再让上游基础信息覆盖
    # 名称/代码/涨跌停; 上游空股本永远不能覆盖这些有效值。
    preserved_rows = _merge_instrument_rows(existing_rows, share_rows)
    all_rows = _merge_instrument_rows(all_rows, fallback_rows, preserved_rows)
    all_rows = [
        row for row in all_rows
        if not _is_non_equity_symbol(row.get("symbol", ""))
    ]

    active_symbols = _latest_daily_symbols(data_dir, as_of)
    old_total, old_valid, _ = _share_coverage(existing_rows, active_symbols=active_symbols)
    new_total, new_valid, missing_symbols = _share_coverage(all_rows, active_symbols=active_symbols)
    old_row_total = len([
        row for row in existing_rows
        if not _is_non_equity_symbol(row.get("symbol", ""))
    ])
    new_row_total = len(all_rows)
    # provider 返回空列表/半截列表时也不能用小快照覆盖全市场。
    if old_row_total >= 100 and new_row_total < int(old_row_total * 0.95):
        raise InstrumentSyncError(
            f"instruments 标的数异常下降: old={old_row_total}, new={new_row_total}"
        )
    if old_total >= 100 and old_valid >= int(old_total * 0.95):
        coverage = new_valid / new_total if new_total else 0.0
        if new_total < 100 or coverage < 0.95:
            raise InstrumentSyncError(
                "instruments 股本覆盖率异常: "
                f"old={old_valid}/{old_total}, new={new_valid}/{new_total}, "
                f"missing={missing_symbols[:20]}"
            )
    logger.info(
        "instruments share coverage: active=%d valid=%d/%d total_rows=%d missing=%d",
        len(active_symbols),
        new_valid,
        new_total,
        new_row_total,
        len(missing_symbols),
    )

    base_date, prev_close = _latest_prev_close_map(data_dir, as_of)
    all_rows, limit_stats = sanitize_limit_prices(
        all_rows,
        prev_close_by_symbol=prev_close,
        as_of=as_of,
        base_date=base_date,
    )
    logger.info(
        "instruments limit sanitize: base_date=%s prev_close=%d kept=%d rewritten=%d cleared=%d",
        base_date,
        len(prev_close),
        limit_stats["kept_provider"],
        limit_stats["rewritten"],
        limit_stats["cleared"],
    )

    df = pl.DataFrame(all_rows)
    df = df.with_columns(pl.lit(as_of).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name, 更新 instruments 维表 (兜底补充)。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name,
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
