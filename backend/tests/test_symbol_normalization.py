from __future__ import annotations

import polars as pl

from app.services.symbols import guess_exchange_suffix, normalize_symbol


class _Repo:
    def __init__(self) -> None:
        self._frames = {
            "stock": pl.DataFrame([
                {"symbol": "002491.SZ", "code": "002491", "name": "通鼎互联"},
                {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"},
            ]),
            "etf": pl.DataFrame([
                {"symbol": "589020.SH", "code": "589020", "name": "科创半导体设备ETF鹏华"},
                {"symbol": "159915.SZ", "code": "159915", "name": "创业板ETF"},
            ]),
            "index": pl.DataFrame([
                {"symbol": "000001.SH", "code": "000001", "name": "上证指数"},
            ]),
        }

    def get_instruments_asset(self, asset_type: str):
        return self._frames.get(asset_type, pl.DataFrame())


def test_normalize_symbol_resolves_all_asset_types_by_code() -> None:
    repo = _Repo()

    assert normalize_symbol("002491", repo) == "002491.SZ"
    assert normalize_symbol("589020", repo) == "589020.SH"
    assert normalize_symbol("159915", repo) == "159915.SZ"
    assert normalize_symbol("000001", repo, asset_types=("index", "stock")) == "000001.SH"


def test_normalize_symbol_handles_prefixed_and_fallback_codes() -> None:
    assert normalize_symbol("sh589020") == "589020.SH"
    assert normalize_symbol("sz159915") == "159915.SZ"
    assert normalize_symbol("589021") == "589021.SH"
    assert normalize_symbol("159916") == "159916.SZ"
    assert normalize_symbol("430047") == "430047.BJ"


def test_guess_exchange_suffix_covers_etf_prefixes() -> None:
    assert guess_exchange_suffix("589020") == "SH"
    assert guess_exchange_suffix("510300") == "SH"
    assert guess_exchange_suffix("159915") == "SZ"
    assert guess_exchange_suffix("180101") == "SZ"
