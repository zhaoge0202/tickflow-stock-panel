"""证券代码规范化工具。"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_APP_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_PREFIXED_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_BARE_CODE_RE = re.compile(r"^\d{6}$")


def normalize_symbol(
    value: str | None,
    repo: Any = None,
    *,
    asset_types: Iterable[str] = ("stock", "etf", "index"),
) -> str:
    """把用户输入的证券代码规范成项目内部 symbol.

    裸 6 位代码优先查本地维表, 确保 ETF/指数等标的不会被简单前缀规则
    误分流; 维表缺失时再按 A 股常见代码段兜底.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""

    if _APP_SYMBOL_RE.match(text):
        return text

    prefixed = _PREFIXED_RE.match(text)
    if prefixed:
        suffix, code = prefixed.groups()
        return f"{code}.{suffix}"

    if "." in text:
        code, suffix = text.split(".", 1)
        return f"{code.strip()}.{suffix.strip().upper()}"

    if not _BARE_CODE_RE.match(text):
        return text

    resolved = _lookup_symbol_by_code(repo, text, asset_types)
    if resolved:
        return resolved

    suffix = guess_exchange_suffix(text)
    return f"{text}.{suffix}" if suffix else text


def guess_exchange_suffix(code: str | None) -> str | None:
    """按 A 股/场内基金常见代码前缀推断交易所."""
    text = str(code or "").strip()
    if text.startswith(("5", "6")):
        return "SH"
    if text.startswith(("0", "1", "3")):
        return "SZ"
    if text.startswith(("8", "43", "92")):
        return "BJ"
    return None


def _lookup_symbol_by_code(repo: Any, code: str, asset_types: Iterable[str]) -> str | None:
    if repo is None:
        return None

    for asset_type in asset_types:
        df = _get_instruments(repo, asset_type)
        if df is None or getattr(df, "is_empty", lambda: True)():
            continue
        if "symbol" not in df.columns:
            continue

        try:
            import polars as pl

            if "code" in df.columns:
                hit = df.filter(pl.col("code").cast(pl.Utf8) == code).head(1)
            else:
                hit = df.filter(pl.col("symbol").cast(pl.Utf8).str.starts_with(f"{code}.")).head(1)
            if not hit.is_empty():
                symbol = str(hit["symbol"][0]).strip().upper()
                if symbol:
                    return symbol
        except Exception:
            continue
    return None


def _get_instruments(repo: Any, asset_type: str):
    try:
        if hasattr(repo, "get_instruments_asset"):
            return repo.get_instruments_asset(asset_type)
        if asset_type == "stock" and hasattr(repo, "get_instruments"):
            return repo.get_instruments()
        if asset_type == "etf" and hasattr(repo, "get_etf_instruments"):
            return repo.get_etf_instruments()
        if asset_type == "index" and hasattr(repo, "get_index_instruments"):
            return repo.get_index_instruments()
    except Exception:
        return None
    return None
