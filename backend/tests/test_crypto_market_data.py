"""crypto 资产类别的存储/分区口径测试 (P1)。

provider 的字段映射与时区换算在 tests/test_okx_provider.py; 本文件只覆盖与数据源无关的
契约: 分钟分区键 = 合约日 (北京自然日)、分区 merge-upsert 幂等、仓储按资产类别隔离路由。

口径 (CONTRIBUTING §3.3): 日归属 = 北京自然日 (OKX 官方日线切日, 与项目 CN_TZ 一致);
分钟 datetime 存北京墙钟 naive; volume/amount 保持上游口径, 不按「手」换算。
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.services import crypto_sync
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "SOXL-USDT-SWAP"


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


# ---------------- 分区写入 ----------------


def test_minute_partitions_follow_beijing_day(repo):
    """分区键 = 北京自然日 (OKX 官方日线切日口径): 北京 00:00 是切换点。"""
    df = pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL, SYMBOL, SYMBOL],
        "datetime": [
            datetime(2026, 10, 12, 23, 59),  # 北京 23:59 → 合约日 10-12
            datetime(2026, 10, 13, 0, 0),    # 边界: 合约日 10-13
            datetime(2026, 10, 13, 7, 59),   # 合约日 10-13
            datetime(2026, 10, 13, 23, 59),  # 合约日 10-13
        ],
        "open": [1.0, 2.0, 3.0, 4.0],
        "high": [1.0, 2.0, 3.0, 4.0],
        "low": [1.0, 2.0, 3.0, 4.0],
        "close": [1.0, 2.0, 3.0, 4.0],
        "volume": [1.0, 2.0, 3.0, 4.0],
        "amount": [1.0, 2.0, 3.0, 4.0],
    })
    crypto_sync.write_crypto_minute_partitions(repo, df)

    prev_dir = repo.store.data_dir / "kline_crypto_minute" / "date=2026-10-12"
    today_dir = repo.store.data_dir / "kline_crypto_minute" / "date=2026-10-13"
    assert prev_dir.joinpath("part.parquet").exists()
    assert today_dir.joinpath("part.parquet").exists()
    assert pl.read_parquet(prev_dir / "part.parquet")["datetime"].to_list() == [datetime(2026, 10, 12, 23, 59)]
    assert sorted(pl.read_parquet(today_dir / "part.parquet")["datetime"].to_list()) == [
        datetime(2026, 10, 13, 0, 0), datetime(2026, 10, 13, 7, 59), datetime(2026, 10, 13, 23, 59),
    ]


def test_minute_partitions_merge_upsert_keeps_one_row_per_bar(repo):
    def frame(close):
        return pl.DataFrame({
            "symbol": [SYMBOL], "datetime": [datetime(2026, 10, 13, 9, 0)],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [close],
            "volume": [1.0], "amount": [1.0],
        })

    crypto_sync.write_crypto_minute_partitions(repo, frame(1.0))
    crypto_sync.write_crypto_minute_partitions(repo, frame(2.0))
    stored = pl.read_parquet(repo.store.data_dir / "kline_crypto_minute" / "date=2026-10-13" / "part.parquet")
    assert stored.height == 1
    assert stored["close"].to_list() == [2.0]


def test_minute_partitions_reject_frame_missing_columns(repo):
    with pytest.raises(ValueError):
        crypto_sync.write_crypto_minute_partitions(
            repo, pl.DataFrame({"symbol": [SYMBOL], "datetime": [datetime(2026, 10, 13, 9, 0)]})
        )


# ---------------- 仓储路由 ----------------


def test_crypto_symbol_resolves_to_crypto_asset_type(repo):
    repo.save_crypto_instruments(pl.DataFrame({
        "symbol": [SYMBOL], "name": ["SOXL"], "code": [SYMBOL],
        "exchange": ["BINANCE"], "asset_type": ["crypto"], "source": ["binance"],
    }))
    assert repo.resolve_asset_type(SYMBOL) == "crypto"
    # A 股未登记代码仍走 stock 兜底 (历史行为不变)
    assert repo.resolve_asset_type("600519.SH") == "stock"
    assert repo.get_instruments_asset("crypto").height == 1


def test_unknown_symbol_still_falls_back_to_stock_without_crypto_instruments(repo):
    assert repo.resolve_asset_type(SYMBOL) == "stock"


def test_crypto_daily_written_and_read_back(repo):
    rows = pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL],
        "date": [date(2026, 10, 12), date(2026, 10, 13)],
        "open": [10.0, 10.5], "high": [11.0, 11.5], "low": [9.5, 10.0],
        "close": [10.5, 11.0], "volume": [100.0, 200.0], "amount": [1050.0, 2200.0],
    })
    repo.append_crypto_daily(rows)
    back = repo.get_crypto_daily(SYMBOL, date(2026, 10, 1), date(2026, 10, 31))
    assert back.height == 2
    assert back["date"].to_list() == [date(2026, 10, 12), date(2026, 10, 13)]
    assert back["volume"].to_list() == [100.0, 200.0]


def test_crypto_minute_glob_is_isolated_from_a_share_minute(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    assert repo._minute_glob_for("crypto").endswith("kline_crypto_minute/**/*.parquet")
    assert repo._minute_glob_for("stock").endswith("kline_minute/**/*.parquet")
    assert repo._minute_glob_for("etf").endswith("kline_etf_minute/**/*.parquet")


def test_crypto_enriched_dir_is_separate_from_a_share():
    from app.tickflow.repository import enriched_dirname

    assert enriched_dirname("crypto") == "kline_crypto_enriched"
    assert enriched_dirname("stock") == "kline_daily_enriched"
