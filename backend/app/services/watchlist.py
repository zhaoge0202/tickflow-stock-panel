"""自选股与分组服务。

自选存储于 ``data/user_data/watchlist.parquet``，分组定义存储于同目录的
``watchlist_groups.json``。历史 Parquet 缺少 ``group_id`` 时按未分组读取。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path

import polars as pl

from app.config import settings
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, resolve_limit

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_MAX_GROUP_NAME_LENGTH = 24
DEFAULT_GROUP_COLOR = "sky"
GROUP_COLORS = frozenset({
    "sky",
    "blue",
    "indigo",
    "violet",
    "fuchsia",
    "rose",
    "orange",
    "amber",
    "lime",
    "emerald",
    "teal",
    "cyan",
})
_ENTRY_SCHEMA = {
    "symbol": pl.Utf8,
    "added_at": pl.Utf8,
    "note": pl.Utf8,
    "group_id": pl.Utf8,
}


def _path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _groups_path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist_groups.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty_entries() -> pl.DataFrame:
    return pl.DataFrame(schema=_ENTRY_SCHEMA)


def _read_entries() -> pl.DataFrame:
    p = _path()
    if not p.exists():
        return _empty_entries()
    df = pl.read_parquet(p)
    defaults = {"symbol": "", "added_at": "", "note": "", "group_id": None}
    for column, dtype in _ENTRY_SCHEMA.items():
        if column not in df.columns:
            df = df.with_columns(pl.lit(defaults[column], dtype=dtype).alias(column))
    return df.select(list(_ENTRY_SCHEMA))


def _write_entries(df: pl.DataFrame) -> None:
    p = _path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    df.select(list(_ENTRY_SCHEMA)).write_parquet(tmp)
    os.replace(tmp, p)


def _read_groups() -> list[dict]:
    p = _groups_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("自选分组配置损坏，请检查 watchlist_groups.json") from exc
    if not isinstance(raw, list):
        raise ValueError("自选分组配置格式不正确")
    groups = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        color = str(item.get("color", DEFAULT_GROUP_COLOR))
        groups.append({
            "id": str(item["id"]),
            "name": str(item["name"]),
            "color": color if color in GROUP_COLORS else DEFAULT_GROUP_COLOR,
        })
    return groups


def _write_groups(groups: list[dict]) -> None:
    p = _groups_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _normalize_group_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("分组名称不能为空")
    if len(normalized) > _MAX_GROUP_NAME_LENGTH:
        raise ValueError(f"分组名称不能超过 {_MAX_GROUP_NAME_LENGTH} 个字符")
    return normalized


def _normalize_group_color(color: str | None) -> str:
    normalized = (color or DEFAULT_GROUP_COLOR).strip().lower()
    if normalized not in GROUP_COLORS:
        raise ValueError("不支持的分组颜色")
    return normalized


def _validate_group_id(group_id: str | None, groups: list[dict]) -> None:
    if group_id is not None and not any(group["id"] == group_id for group in groups):
        raise ValueError("自选分组不存在")


def list_symbols() -> list[dict]:
    with _LOCK:
        df = _read_entries()
        return [] if df.is_empty() else df.to_dicts()


def add(symbol: str, note: str = "", group_id: str | None = None) -> list[dict]:
    rows, _ = add_batch([symbol], note=note, group_id=group_id)
    return rows


def add_batch(
    symbols: list[str],
    note: str = "",
    group_id: str | None = None,
) -> tuple[list[dict], int]:
    """批量添加并保持既有语义：每个新处理的标的移动到列表最前面。"""
    with _LOCK:
        groups = _read_groups()
        _validate_group_id(group_id, groups)
        rows = _read_entries().to_dicts()
        added = 0
        for symbol in symbols:
            existing = next((row for row in rows if row["symbol"] == symbol), None)
            if existing is None:
                added += 1
            rows = [row for row in rows if row["symbol"] != symbol]
            resolved_group_id = (
                group_id if group_id is not None else (existing or {}).get("group_id")
            )
            rows.insert(0, {
                "symbol": symbol,
                "added_at": datetime.utcnow().isoformat(timespec="seconds"),
                "note": note,
                "group_id": resolved_group_id,
            })
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA) if rows else _empty_entries()
        _write_entries(out)
        return out.to_dicts(), added


def remove(symbol: str) -> list[dict]:
    with _LOCK:
        df = _read_entries().filter(pl.col("symbol") != symbol)
        _write_entries(df)
        return df.to_dicts()


def move_to_top(symbol: str) -> list[dict]:
    with _LOCK:
        df = _read_entries()
        if df.is_empty() or symbol not in df["symbol"].to_list():
            return df.to_dicts()
        target = df.filter(pl.col("symbol") == symbol)
        rest = df.filter(pl.col("symbol") != symbol)
        out = pl.concat([target, rest], how="diagonal_relaxed")
        _write_entries(out)
        return out.to_dicts()


def clear() -> int:
    """清空自选列表。返回移除的数量。"""
    with _LOCK:
        df = _read_entries()
        count = df.height
        if count > 0:
            _write_entries(_empty_entries())
        return count


def list_groups() -> list[dict]:
    with _LOCK:
        return _read_groups()


def create_group(name: str, color: str | None = None) -> tuple[list[dict], dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        normalized_color = _normalize_group_color(color)
        groups = _read_groups()
        if any(group["name"].casefold() == normalized.casefold() for group in groups):
            raise ValueError("分组名称已存在")
        group = {
            "id": uuid.uuid4().hex,
            "name": normalized,
            "color": normalized_color,
        }
        groups.append(group)
        _write_groups(groups)
        return groups, group


def rename_group(group_id: str, name: str, color: str | None = None) -> list[dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        groups = _read_groups()
        target = next((group for group in groups if group["id"] == group_id), None)
        if target is None:
            raise KeyError(group_id)
        if any(
            group["id"] != group_id and group["name"].casefold() == normalized.casefold()
            for group in groups
        ):
            raise ValueError("分组名称已存在")
        target["name"] = normalized
        if color is not None:
            target["color"] = _normalize_group_color(color)
        _write_groups(groups)
        return groups


def delete_group(group_id: str) -> tuple[list[dict], list[dict]]:
    """删除分组定义，原分组内的自选保留并转为未分组。"""
    with _LOCK:
        groups = _read_groups()
        if not any(group["id"] == group_id for group in groups):
            raise KeyError(group_id)
        df = _read_entries().with_columns(
            pl.when(pl.col("group_id") == group_id)
            .then(None)
            .otherwise(pl.col("group_id"))
            .alias("group_id")
        )
        remaining = [group for group in groups if group["id"] != group_id]
        _write_entries(df)
        _write_groups(remaining)
        return remaining, df.to_dicts()


def set_group(symbol: str, group_id: str | None) -> list[dict]:
    with _LOCK:
        groups = _read_groups()
        _validate_group_id(group_id, groups)
        df = _read_entries()
        if symbol not in df["symbol"].to_list():
            raise KeyError(symbol)
        df = df.with_columns(
            pl.when(pl.col("symbol") == symbol)
            .then(pl.lit(group_id, dtype=pl.Utf8))
            .otherwise(pl.col("group_id"))
            .alias("group_id")
        )
        _write_entries(df)
        return df.to_dicts()


def fetch_quotes(symbols: list[str], capset: CapabilitySet, timeout_s: float = 8.0) -> list[dict]:
    """拉取实时行情。

    优先用 quote.batch;否则降级为 quote.by_symbol 单股请求。
    timeout_s: 单批次请求超时(秒)，防止 API 卡死阻塞整个请求。
    """
    if not symbols:
        return []

    tf = get_client()
    quotes: list[dict] = []

    # 走 batch
    if capset.has(Cap.QUOTE_BATCH):
        batch_size = resolve_limit(capset, Cap.QUOTE_BATCH, default_batch=50).batch
    elif capset.has(Cap.QUOTE_BY_SYMBOL):
        batch_size = resolve_limit(capset, Cap.QUOTE_BY_SYMBOL, default_batch=5).batch
    else:
        # 无任何实时行情能力(none/free 档走 free-api 服务器,不提供实时行情)
        # 提前返回空,避免发起注定失败的请求
        return []

    chunks = chunked(symbols, batch_size)

    # 用线程池为每个批次加超时保护
    pool = ThreadPoolExecutor(max_workers=1)
    for chunk in chunks:
        try:
            future = pool.submit(tf.quotes.get, symbols=chunk, as_dataframe=True)
            raw = future.result(timeout=timeout_s)
            if raw is None or len(raw) == 0:
                continue
            df = pl.from_pandas(raw)
            rename_map = {
                "last_price": "price",
                "ext.change_pct": "pct",
                "ext.name": "name",
            }
            df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
            quotes.extend(df.to_dicts())
        except FuturesTimeout:
            logger.warning("quote fetch timeout (%.1fs) for %d symbols", timeout_s, len(chunk))
            break  # 超时后不再尝试后续批次
        except Exception as e:  # noqa: BLE001
            logger.warning("quote fetch failed for %d symbols: %s", len(chunk), e)
    pool.shutdown(wait=False)

    return quotes
