"""涨停梯队的「昨日连板数」不能按固定自然日窗口回看。

load_prior_consecutive 原本在 as_of 前 1~9 个自然日里找分区。春节长假连着
调休周末, 相邻两个交易日能隔 10~11 个自然日 (如 2024-02-08 到 2024-02-19),
窗口整段落空, prev_consec 全被填 0: 断板(晋级失败)一栏空掉, 炸板股的板数
从「昨日 N 板 + 1」退回 1 板。
"""
from __future__ import annotations

import types
from datetime import date

import polars as pl
import pytest

from app.services.screener import ScreenerService

_CONSEC_COL = "consecutive_limit_ups"


def _write_partition(data_dir, day: date, rows: dict) -> None:
    part = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(part / "part.parquet")


def _service(data_dir) -> ScreenerService:
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=data_dir))
    return ScreenerService(repo)


@pytest.mark.parametrize(
    ("prev_day", "as_of", "label"),
    [
        (date(2024, 2, 8), date(2024, 2, 19), "春节长假(11 个自然日)"),
        (date(2023, 1, 20), date(2023, 1, 30), "春节长假(10 个自然日)"),
        (date(2026, 6, 26), date(2026, 6, 29), "周末(3 个自然日)"),
        (date(2026, 6, 29), date(2026, 6, 30), "相邻交易日"),
    ],
)
def test_prior_consecutive_spans_any_holiday_gap(tmp_path, prev_day, as_of, label):
    _write_partition(tmp_path, prev_day, {"symbol": ["600000.SH"], _CONSEC_COL: [3]})
    _write_partition(tmp_path, as_of, {"symbol": ["600000.SH"], _CONSEC_COL: [0]})

    got = _service(tmp_path).load_prior_consecutive(as_of, _CONSEC_COL)

    assert not got.is_empty(), f"{label}: 没找到前一交易日分区"
    assert got.to_dicts() == [{"symbol": "600000.SH", "prev_consec": 3}]


def test_prior_consecutive_picks_the_nearest_earlier_partition(tmp_path):
    """多个更早分区时取最近的一个, 不是最早的。"""
    _write_partition(tmp_path, date(2024, 2, 6), {"symbol": ["600000.SH"], _CONSEC_COL: [1]})
    _write_partition(tmp_path, date(2024, 2, 7), {"symbol": ["600000.SH"], _CONSEC_COL: [2]})
    _write_partition(tmp_path, date(2024, 2, 8), {"symbol": ["600000.SH"], _CONSEC_COL: [3]})
    _write_partition(tmp_path, date(2024, 2, 19), {"symbol": ["600000.SH"], _CONSEC_COL: [0]})

    got = _service(tmp_path).load_prior_consecutive(date(2024, 2, 19), _CONSEC_COL)

    assert got.to_dicts() == [{"symbol": "600000.SH", "prev_consec": 3}]


def test_prior_consecutive_ignores_as_of_and_future_partitions(tmp_path):
    """只看严格早于 as_of 的分区; 当日和更晚的分区不能被当成「昨日」。"""
    _write_partition(tmp_path, date(2026, 6, 30), {"symbol": ["600000.SH"], _CONSEC_COL: [5]})
    _write_partition(tmp_path, date(2026, 7, 1), {"symbol": ["600000.SH"], _CONSEC_COL: [6]})

    got = _service(tmp_path).load_prior_consecutive(date(2026, 6, 30), _CONSEC_COL)

    assert got.is_empty()


def test_prior_consecutive_skips_partition_missing_the_column(tmp_path):
    """前一天分区缺列时继续往前找 (保持旧循环行为)。"""
    _write_partition(tmp_path, date(2026, 6, 26), {"symbol": ["600000.SH"], _CONSEC_COL: [4]})
    _write_partition(tmp_path, date(2026, 6, 29), {"symbol": ["600000.SH"], "close": [10.0]})
    _write_partition(tmp_path, date(2026, 6, 30), {"symbol": ["600000.SH"], _CONSEC_COL: [0]})

    got = _service(tmp_path).load_prior_consecutive(date(2026, 6, 30), _CONSEC_COL)

    assert got.to_dicts() == [{"symbol": "600000.SH", "prev_consec": 4}]


def test_prior_consecutive_returns_empty_without_any_partition(tmp_path):
    """本地没有 enriched 目录时返回空表, 不抛异常。"""
    got = _service(tmp_path).load_prior_consecutive(date(2026, 6, 30), _CONSEC_COL)

    assert got.is_empty()
