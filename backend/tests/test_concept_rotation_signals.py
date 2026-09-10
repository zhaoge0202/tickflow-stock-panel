"""概念/行业轮动信号预计算 (_compute_rotation_signals) 单元测试。"""
from __future__ import annotations

from app.services.concept_rotation_analyzer import _compute_rotation_signals

# dates 与服务口径一致: 最新在最前
_DATES = ["2026-01-09", "2026-01-08", "2026-01-07", "2026-01-06", "2026-01-05"]


def _column(names_pcts: list[tuple[str, float]]) -> list[list]:
    return [[name, pct] for name, pct in names_pcts]


def _baseline_columns() -> dict[str, list[list]]:
    """每日 40 个陪跑概念, 排名稳定, 用于把待测概念挤到指定名次。"""
    filler = [(f"陪跑{i:02d}", 0.01 - i * 0.0001) for i in range(40)]
    return {d: _column(filler) for d in _DATES}


def test_missing_early_days_do_not_shift_ranks_forward():
    """概念只在最近两日出现时, 缺失日必须补在时间轴左端 (早期), 不能补在右端。

    补位若一律追加到列表末尾, "只在最近两日上榜且排第一"的新晋概念会被读成
    "早期第一、最新掉出榜外", 从而被误判为退潮。
    """
    columns = _baseline_columns()
    for d, pct in (("2026-01-09", 0.09), ("2026-01-08", 0.08)):
        columns[d] = _column([("新晋题材", pct)]) + columns[d]

    signals = _compute_rotation_signals(_DATES, columns)
    by_name = {
        item["concept"]: item
        for group in signals.values()
        for item in group
    }
    assert "新晋题材" in by_name
    # ranks[0]=最早日, ranks[-1]=最新日: 最早两日缺席(999), 最近两日排第一
    assert by_name["新晋题材"]["ranks"] == [999, 999, 999, 1, 1]

    rising = [item["concept"] for item in signals["rising"]]
    fading = [item["concept"] for item in signals["fading"]]
    assert "新晋题材" in rising
    assert "新晋题材" not in fading


def test_missing_recent_days_pad_at_the_right_end():
    """概念只在最早两日出现时, 缺失日补在时间轴右端 (最新)。"""
    columns = _baseline_columns()
    for d, pct in (("2026-01-05", 0.09), ("2026-01-06", 0.08)):
        columns[d] = _column([("退潮题材", pct)]) + columns[d]

    signals = _compute_rotation_signals(_DATES, columns)
    by_name = {
        item["concept"]: item
        for group in signals.values()
        for item in group
    }
    assert "退潮题材" in by_name
    assert by_name["退潮题材"]["ranks"] == [1, 1, 999, 999, 999]
    assert "退潮题材" in [item["concept"] for item in signals["fading"]]
