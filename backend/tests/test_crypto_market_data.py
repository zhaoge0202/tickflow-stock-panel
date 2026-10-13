"""crypto 资产类别接入测试 (P1: 维表 / 日K / 分钟 / 资产类型判定)。

核心口径 (CONTRIBUTING §3.2 / §3.3):
  - 日K 的 date 是 UTC 合约日 (与交易所 K 线一致), 不是北京自然日;
  - 分钟 datetime 是北京墙钟 naive (项目契约), 分区键是 UTC 合约日;
  - volume/amount 保持上游口径, 不按「手」(1 手 = 100 股) 换算;
  - crypto symbol 必须被判定为 crypto, 不得落到 stock 兜底 (否则吃满 A 股规则)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.data_providers.binance.client import BinanceClientError
from app.data_providers.binance.provider import (
    BinanceProvider,
    utc_ms_to_cn_wallclock,
    utc_ms_to_contract_date,
)
from app.services import crypto_sync
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "SOXLUSDT"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _kline(open_time: datetime, *, o=10.0, h=11.0, low=9.5, c=10.5, vol=1234.5, quote=12962.25) -> list:
    return [
        _ms(open_time), str(o), str(h), str(low), str(c), str(vol),
        _ms(open_time + timedelta(days=1)) - 1, str(quote), 88,
    ]


class FakeClient:
    """最小假客户端: 只实现 provider 真正调用的两个方法。"""

    def __init__(self, info=None, klines_by_symbol=None) -> None:
        self._info = info if info is not None else {"symbols": []}
        self._klines = klines_by_symbol or {}
        self.calls: list[tuple] = []

    def exchange_info(self):
        return self._info

    def klines(self, symbol, interval="1d", *, start_ms=None, end_ms=None, limit=None):
        self.calls.append((symbol, interval, start_ms, end_ms, limit))
        if symbol not in self._klines:
            raise BinanceClientError(f"no data for {symbol}")
        return self._klines[symbol]


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


# ---------------- 时间换算 ----------------


def test_utc_ms_maps_to_cn_wallclock_and_utc_contract_day():
    # UTC 2026-10-13 00:00 → 北京 08:00 同一时刻; 合约日 = UTC 日期 10-13
    open_time = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)
    assert utc_ms_to_cn_wallclock(_ms(open_time)) == datetime(2026, 10, 13, 8, 0)
    assert utc_ms_to_contract_date(_ms(open_time)) == date(2026, 10, 13)
    # UTC 2026-10-13 23:30 → 北京 10-14 07:30, 但合约日仍是 10-13
    late = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)
    assert utc_ms_to_cn_wallclock(_ms(late)) == datetime(2026, 10, 14, 7, 30)
    assert utc_ms_to_contract_date(_ms(late)) == date(2026, 10, 13)


# ---------------- 维表 ----------------


def test_instruments_keep_only_usdt_perpetual_trading():
    info = {"symbols": [
        {"symbol": SYMBOL, "baseAsset": "SOXL", "quoteAsset": "USDT",
         "contractType": "PERPETUAL", "status": "TRADING"},
        # 币本位 / 交割 / 已下架 / 非 USDT 一律剔除
        {"symbol": "BTCUSD_PERP", "baseAsset": "BTC", "quoteAsset": "USD",
         "contractType": "PERPETUAL", "status": "TRADING"},
        {"symbol": "SOXLUSDT_2601", "baseAsset": "SOXL", "quoteAsset": "USDT",
         "contractType": "CURRENT_QUARTER", "status": "TRADING"},
        {"symbol": "OLDUSDT", "baseAsset": "OLD", "quoteAsset": "USDT",
         "contractType": "PERPETUAL", "status": "SETTLING"},
    ]}
    rows = BinanceProvider(FakeClient(info=info)).get_crypto_instruments()
    assert [r["symbol"] for r in rows] == [SYMBOL]
    assert rows[0]["asset_type"] == "crypto"
    assert rows[0]["exchange"] == "BINANCE"
    # 代码保持上游原样 (含 USDT 后缀), 不套 A 股交易所后缀规则
    assert "." not in rows[0]["symbol"]


def test_empty_exchange_info_raises_instead_of_silent_empty():
    with pytest.raises(BinanceClientError):
        BinanceProvider(FakeClient(info={"symbols": []})).get_crypto_instruments()


# ---------------- 日K ----------------


def test_daily_rows_use_utc_contract_day_and_passthrough_units():
    client = FakeClient(klines_by_symbol={
        SYMBOL: [
            _kline(datetime(2026, 10, 12, 0, 0, tzinfo=UTC), c=10.0, vol=100.0, quote=1000.0),
            _kline(datetime(2026, 10, 13, 0, 0, tzinfo=UTC), c=10.5, vol=1234.5, quote=12962.25),
        ],
    })
    df = BinanceProvider(client).get_daily([SYMBOL], start_date=date(2026, 10, 1), end_date=date(2026, 10, 13))
    assert df["date"].to_list() == [date(2026, 10, 12), date(2026, 10, 13)]
    # 单位透传: volume 不是「手」, 不得放大或缩小 100 倍
    assert df["volume"].to_list() == [100.0, 1234.5]
    assert df["amount"].to_list() == [1000.0, 12962.25]
    assert df["close"].to_list() == [10.0, 10.5]


def test_daily_single_symbol_failure_is_soft():
    client = FakeClient(klines_by_symbol={
        SYMBOL: [_kline(datetime(2026, 10, 13, 0, 0, tzinfo=UTC))],
    })
    # BTCUSDT 不在假数据里 → 该标的抛错, 其余标的仍返回, 不整体失败
    df = BinanceProvider(client).get_daily([SYMBOL, "BTCUSDT"])
    assert df["symbol"].to_list() == [SYMBOL]


# ---------------- 分钟K ----------------


def test_minute_datetime_is_cn_wallclock():
    open_time = datetime(2026, 10, 13, 1, 30, tzinfo=UTC)  # 北京 09:30
    client = FakeClient(klines_by_symbol={SYMBOL: [_kline(open_time, vol=7.5, quote=78.0)]})
    df = BinanceProvider(client).get_minute([SYMBOL])
    assert df["datetime"].to_list() == [datetime(2026, 10, 13, 9, 30)]
    assert df["volume"].to_list() == [7.5]


def test_minute_window_is_interpreted_as_cn_wallclock():
    client = FakeClient(klines_by_symbol={SYMBOL: []})
    provider = BinanceProvider(client)
    provider.get_minute(
        [SYMBOL],
        start_time=datetime(2026, 10, 13, 8, 0),
        end_time=datetime(2026, 10, 14, 8, 0),
    )
    _, _, start_ms, end_ms, _ = client.calls[0]
    # 北京 08:00 → UTC 00:00
    assert start_ms == _ms(datetime(2026, 10, 13, 0, 0, tzinfo=UTC))
    assert end_ms == _ms(datetime(2026, 10, 14, 0, 0, tzinfo=UTC))


# ---------------- 分区写入 ----------------


def test_minute_partitions_follow_contract_day_not_cn_day(repo):
    """北京 00:00-08:00 的分钟必须落进前一个合约日分区。"""
    df = pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL, SYMBOL],
        "datetime": [
            datetime(2026, 10, 13, 7, 59),   # 北京 07:59 → 合约日 10-12
            datetime(2026, 10, 13, 8, 0),    # 边界: 合约日 10-13
            datetime(2026, 10, 13, 23, 59),  # 合约日 10-13
        ],
        "open": [1.0, 2.0, 3.0],
        "high": [1.0, 2.0, 3.0],
        "low": [1.0, 2.0, 3.0],
        "close": [1.0, 2.0, 3.0],
        "volume": [1.0, 2.0, 3.0],
        "amount": [1.0, 2.0, 3.0],
    })
    crypto_sync.write_crypto_minute_partitions(repo, df)

    prev_dir = repo.store.data_dir / "kline_crypto_minute" / "date=2026-10-12"
    today_dir = repo.store.data_dir / "kline_crypto_minute" / "date=2026-10-13"
    assert prev_dir.joinpath("part.parquet").exists()
    assert today_dir.joinpath("part.parquet").exists()
    assert pl.read_parquet(prev_dir / "part.parquet")["datetime"].to_list() == [datetime(2026, 10, 13, 7, 59)]
    assert sorted(pl.read_parquet(today_dir / "part.parquet")["datetime"].to_list()) == [
        datetime(2026, 10, 13, 8, 0), datetime(2026, 10, 13, 23, 59),
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
