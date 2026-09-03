"""max_hold_days 强制退出矩阵回归测试(issue #198)。

_build_max_hold_exits 是 /api/backtest/run 里 max_hold_days 强制平仓的纯逻辑,
不依赖 vectorbt, 可独立断言。覆盖两处历史缺陷:
1. 链式 `iloc[row][col] = True` 在 pandas CoW 下写入丢失 → 强制退出信号从不生效。
2. 以 `entries.copy()` 起步 → 把入场位当退出位, 入场当日即被平仓。
"""
from __future__ import annotations

import pandas as pd

from app.services.backtest import _build_max_hold_exits


def _entries(data: dict, n: int) -> pd.DataFrame:
    return pd.DataFrame(data, index=pd.RangeIndex(n)).astype(bool)


def test_forced_exit_placed_max_hold_days_after_entry():
    """入场后第 max_hold_days 个交易日置强制退出(核心: 该单元格必须真的被写入)。"""
    entries = _entries({"A": [True, False, False, False, False]}, 5)
    out = _build_max_hold_exits(entries, 2)
    assert out["A"].tolist() == [False, False, True, False, False]


def test_does_not_mark_entry_bar_as_exit():
    """回归: 强制退出矩阵不得包含入场位本身。"""
    entries = _entries({"A": [True, False, False]}, 3)
    out = _build_max_hold_exits(entries, 1)
    assert out["A"].tolist() == [False, True, False]


def test_end_index_clamped_to_last_row():
    """入场后越界时 clamp 到最后一根 K。"""
    entries = _entries({"A": [False, False, False, True, False]}, 5)
    out = _build_max_hold_exits(entries, 5)  # 3+5 越界 → clamp 到 4
    assert out["A"].tolist() == [False, False, False, False, True]


def test_entry_on_last_row_produces_no_exit():
    """入场即最后一根 K 时 end_i == i, 不产生退出(避免同根自相矛盾)。"""
    entries = _entries({"A": [False, False, True]}, 3)
    out = _build_max_hold_exits(entries, 2)
    assert out["A"].tolist() == [False, False, False]


def test_multiple_entries_single_column():
    entries = _entries({"A": [True, False, True, False, False]}, 5)
    out = _build_max_hold_exits(entries, 1)
    assert out["A"].tolist() == [False, True, False, True, False]


def test_multiple_columns_independent():
    entries = _entries({"A": [True, False, False], "B": [False, True, False]}, 3)
    out = _build_max_hold_exits(entries, 1)
    assert out["A"].tolist() == [False, True, False]
    assert out["B"].tolist() == [False, False, True]
    assert list(out.columns) == ["A", "B"]
    assert out.index.equals(entries.index)
