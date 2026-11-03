"""分钟K分区路径解析 — 必须与数据目录按 Path 语义拼接, 不能被平台分隔符左右。

回归背景: 旧实现用 glob 字符串 `rsplit("/", 2)` 截取 date= 分区根目录。
Windows 上 glob 是反斜杠路径, rsplit 什么都切不掉, 拼出来的
`{data_dir}/kline_minute/**/*.parquet/date=2026-10-30/part.parquet` 永远不存在,
`get_minute_by_dates` 因此对每一天都返回空表 —— 分钟策略回测在 Windows
上表现为「区间内未产生买入信号 (区间内 N 个交易日缺分钟K分区被跳过)」,
而实际分区就在盘上。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 1, 5)
SYMBOL = "600000.SH"


def _write_minute_partition(data_dir, day: date, symbol: str = SYMBOL) -> None:
    part = data_dir / "kline_minute" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol, symbol],
        "datetime": [datetime(day.year, day.month, day.day, 9, 31),
                     datetime(day.year, day.month, day.day, 9, 32)],
        "open": [10.0, 10.1],
        "high": [10.2, 10.3],
        "low": [9.9, 10.0],
        "close": [10.1, 10.2],
        "volume": [1000.0, 1200.0],
        "amount": [1.01e6, 1.22e6],
    }).write_parquet(part / "part.parquet")


def test_minute_dir_points_at_real_partition_root(tmp_path):
    _write_minute_partition(tmp_path, DAY)
    repo = KlineRepository(DataStore(tmp_path))

    assert repo._minute_dir("stock") == tmp_path / "kline_minute"
    # 分区目录必须能在该根目录下被枚举出来
    assert (repo._minute_dir("stock") / f"date={DAY.isoformat()}" / "part.parquet").exists()


def test_get_minute_by_dates_reads_partition(tmp_path):
    _write_minute_partition(tmp_path, DAY)
    repo = KlineRepository(DataStore(tmp_path))

    assert repo.list_minute_dates(DAY, DAY, "stock") == [DAY]
    df = repo.get_minute_by_dates([SYMBOL], [DAY], "stock")
    assert df.height == 2
    assert df["symbol"].unique().to_list() == [SYMBOL]


def test_minute_partition_path_resolves_day(tmp_path):
    _write_minute_partition(tmp_path, DAY)
    repo = KlineRepository(DataStore(tmp_path))

    part = repo._minute_partition_path("stock", DAY)
    assert part is not None
    assert repo._minute_partition_path("stock", date(2026, 1, 6)) is None
