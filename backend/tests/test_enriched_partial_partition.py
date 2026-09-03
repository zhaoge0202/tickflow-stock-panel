"""#223 回归: 盘后管道必须识别并重算覆盖不全的 enriched 分区。

自选实时路径 (merge_live_enriched_asset) 会在全市场 enriched 生成前提前
创建当日分区 (只有几只自选); 仅按日期目录计数比较会把它误判为完整分区
而跳过计算, 造成日K连续缺失与均线错误。_prune_partial_enriched_partitions
按同日 daily/enriched 行数比较, 发现部分分区即删除, 让增量重算按"新日期"
全市场补齐。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.jobs.daily_pipeline import _prune_partial_enriched_partitions


def _write_partition(base: Path, day: str, symbols: list[str]) -> None:
    part = base / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols, "close": [1.0] * len(symbols)}).write_parquet(
        part / "part.parquet"
    )


def test_partial_partition_is_pruned(tmp_path) -> None:
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    # 2026-08-28: daily 全市场 100 只, enriched 只有实时写入的 5 只 (issue 实测形态)
    _write_partition(daily, "2026-08-28", [f"s{i:06d}" for i in range(100)])
    _write_partition(enriched, "2026-08-28", [f"s{i:06d}" for i in range(5)])
    # 前一日两边都是完整的 → 不动
    _write_partition(daily, "2026-08-27", ["a", "b"])
    _write_partition(enriched, "2026-08-27", ["a", "b"])

    pruned = _prune_partial_enriched_partitions(daily, enriched)

    assert pruned == ["2026-08-28"]
    assert not (enriched / "date=2026-08-28").exists()
    assert (enriched / "date=2026-08-27").exists()  # 完整分区保留


def test_complete_partitions_untouched(tmp_path) -> None:
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    syms = [f"s{i:06d}" for i in range(50)]
    _write_partition(daily, "2026-09-01", syms)
    _write_partition(enriched, "2026-09-01", syms)

    assert _prune_partial_enriched_partitions(daily, enriched) == []
    assert (enriched / "date=2026-09-01" / "part.parquet").exists()


def test_enriched_date_without_daily_is_left_alone(tmp_path) -> None:
    # 今日日K尚未同步时, 实时创建的当日分区留给当日正常流程处理
    daily = tmp_path / "kline_daily"
    enriched = tmp_path / "kline_daily_enriched"
    _write_partition(enriched, str(date.today()), ["only_watchlist"])

    assert _prune_partial_enriched_partitions(daily, enriched) == []
    assert (enriched / f"date={date.today()}").exists()
