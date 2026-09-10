"""收盘价过期分区回归: 盘后管道必须重算"行数完整但价格停留在竞价前"的 enriched 分区。

实时 flush 可能在收盘集合竞价结果发布前写入当日分区 (实测 2026-09-04: TickFlow
实时端点收盘后仍返回旧价, 3392/5554 只股票 enriched 收盘价与官方日线不符),
分区行数与 daily 相同, #223 的行数校验识别不到, 增量路径不会重算, 当日偏离值/
动量等全部 enriched 消费方都会用旧价 (海鸥住工 10 日偏离 98.77% vs 官方 99.61%)。
_prune_stale_price_partitions 按官方日线做 raw_close vs close 值级比对, 不一致即
删分区, 让增量重算按官方日线全市场重建。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.jobs.daily_pipeline import _prune_stale_price_partitions


def _write_partition(base: Path, day: str, symbols: list[str], closes: list[float], col: str) -> None:
    part = base / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols, col: closes}).write_parquet(part / "part.parquet")


def test_stale_close_partition_is_pruned(tmp_path) -> None:
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    syms = ["002084.SZ", "600519.SH", "000001.SZ"]
    # 2026-09-04: 官方日线收盘 7.10, 实时写入的 enriched 停留在竞价前 7.07;
    # 行数两侧一致 (覆盖全), #223 行数校验识别不到 (issue 实测形态)
    _write_partition(daily, "2026-09-04", syms, [7.10, 1330.10, 11.89], "close")
    _write_partition(enriched, "2026-09-04", syms, [7.07, 1330.10, 11.89], "raw_close")

    pruned = _prune_stale_price_partitions(daily, enriched)

    assert pruned == ["2026-09-04"]
    assert not (enriched / "date=2026-09-04").exists()


def test_matching_close_partitions_untouched(tmp_path) -> None:
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    syms = ["002084.SZ", "600519.SH"]
    _write_partition(daily, "2026-09-04", syms, [7.10, 1330.10], "close")
    _write_partition(enriched, "2026-09-04", syms, [7.10, 1330.10], "raw_close")

    assert _prune_stale_price_partitions(daily, enriched) == []
    assert (enriched / "date=2026-09-04" / "part.parquet").exists()


def test_multiple_stale_dates_all_pruned(tmp_path) -> None:
    # 管道连续数日未触发重建时, 最近多个交易日的过期分区一并修复
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    for day, stale in [("2026-09-04", 7.07), ("2026-09-03", 6.88), ("2026-09-02", 6.80)]:
        _write_partition(daily, day, ["002084.SZ"], [stale + 0.03], "close")
        _write_partition(enriched, day, ["002084.SZ"], [stale], "raw_close")

    pruned = _prune_stale_price_partitions(daily, enriched)

    assert sorted(pruned) == ["2026-09-02", "2026-09-03", "2026-09-04"]


def test_missing_raw_close_column_left_alone(tmp_path) -> None:
    # 旧 schema 无 raw_close 列 → 读列失败, 交给既有完整性检查, 不误删
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    _write_partition(daily, "2026-09-04", ["002084.SZ"], [7.10], "close")
    part = enriched / "date=2026-09-04"
    part.mkdir(parents=True)
    pl.DataFrame({"symbol": ["002084.SZ"], "close": [7.07]}).write_parquet(part / "part.parquet")

    assert _prune_stale_price_partitions(daily, enriched) == []
    assert (part / "part.parquet").exists()


def test_daily_partition_missing_left_alone(tmp_path) -> None:
    # 官方日线尚未同步的日期不比对 (留给当日正常流程)
    enriched = tmp_path / "kline_daily_enriched"
    _write_partition(enriched, "2026-09-04", ["002084.SZ"], [7.07], "raw_close")

    assert _prune_stale_price_partitions(tmp_path / "kline_daily", enriched) == []
    assert (enriched / "date=2026-09-04" / "part.parquet").exists()
