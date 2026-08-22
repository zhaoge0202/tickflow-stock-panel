from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np

RegimePoint = tuple[str, float]

REGIME_THREE_LEVEL_MAP = {
    "strong": "strong",
    "lean_strong": "strong",
    "range": "range",
    "lean_weak": "weak",
    "weak": "weak",
}


def three_level_regime(state: str) -> str:
    return REGIME_THREE_LEVEL_MAP.get(state, state)


def _date_text(value: object) -> str:
    return str(value)[:10]


def _normalize_regime_point(value: Any) -> RegimePoint:
    if isinstance(value, Mapping):
        state = str(value.get("state", ""))
        score = float(value.get("score", 0) or 0)
        return state, score
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return str(value[0]), float(value[1] or 0)
    raise ValueError("市场环境数据格式无效")


def clamp_formal_start_for_regime(
    labels: Sequence[str],
    required_start: date | None,
    regime_filter: Mapping[str, Any] | None,
) -> date | None:
    """正式首日无前驱交易日时, 把首日让渡为预热, 返回顺延后的正式起点。

    触发条件: 环境过滤实际启用, 且面板首日 >= 正式起点 (数据边界即正式起点,
    典型如「全部」范围从本地数据第一天开始)。此时首日的 T-1 环境所需的
    上一交易日不在面板内, fail-closed 校验会直接拒绝; 把首日降级为预热、
    正式起点顺延到第二个交易日, 首日环境即成为次日的 T-1。
    其余情况 (有预热日 / 过滤未启用 / 标签不足两天) 原样返回 required_start,
    无法顺延时由后续校验给出明确报错。
    """
    if not regime_filter or required_start is None:
        return required_start
    has_states = bool(regime_filter.get("states") or [])
    has_score = regime_filter.get("min_score") is not None
    if not (has_states or has_score):
        return required_start
    if len(labels) < 2:
        return required_start
    if _date_text(labels[0]) < str(required_start):
        return required_start
    try:
        return date.fromisoformat(_date_text(labels[1]))
    except ValueError:
        return required_start


def align_regime_t_minus_one(
    labels: Sequence[str],
    regime_by_date: Mapping[object, Any],
    required_start: date | None,
    required_end: date | None,
    *,
    first_day_boundary_ok: bool = False,
) -> list[RegimePoint | None]:
    """Align each label with the preceding label's regime without any I/O.

    first_day_boundary_ok: 统计类调用方 (因子环境分组) 允许首日无前驱环境 ——
    数据边界即正式首日 (本地数据从正式首日开始) 时首日没有 T-1 环境属正常,
    跳过首日不参与分组即可, 不应阻断整个回测。内部缺口仍 fail-closed。
    """
    regime_map = {
        _date_text(key): _normalize_regime_point(value)
        for key, value in regime_by_date.items()
    }
    if not regime_map:
        raise ValueError("市场环境数据为空, 请先在数据页完成市场环境计算后再回测")

    aligned: list[RegimePoint | None] = [None] * len(labels)
    required_start_text = str(required_start) if required_start is not None else None
    required_end_text = str(required_end) if required_end is not None else None
    missing_dates: list[str] = []
    if labels and required_start_text is not None and not first_day_boundary_ok:
        first_label = _date_text(labels[0])
        if first_label >= required_start_text and (
            required_end_text is None or first_label <= required_end_text
        ):
            raise ValueError(
                f"市场环境数据覆盖不完整: 正式首日 {first_label} 缺少前一交易日环境, "
                "请把前一交易日行情包含在预热区间"
            )
    for index in range(1, len(labels)):
        current_label = _date_text(labels[index])
        previous_label = _date_text(labels[index - 1])
        point = regime_map.get(previous_label)
        if point is not None:
            aligned[index] = point
            continue
        required = (
            (required_start_text is None or current_label >= required_start_text)
            and (required_end_text is None or current_label <= required_end_text)
        )
        if required:
            missing_dates.append(previous_label)

    if missing_dates:
        first_missing = missing_dates[0]
        suffix = f" 等 {len(missing_dates)} 天" if len(missing_dates) > 1 else ""
        raise ValueError(
            f"市场环境数据覆盖不完整: 缺少前一交易日环境 {first_missing}{suffix}, "
            "请先补算对应区间"
        )
    return aligned


def build_regime_filter_mask(
    labels: Sequence[str],
    regime_filter: Mapping[str, Any] | None,
    regime_by_date: Mapping[object, Any],
    *,
    required_start: date | None = None,
    required_end: date | None = None,
) -> np.ndarray | None:
    """Build a T-1 regime filter mask from caller-supplied regime data.

    States are matched against the raw five-level labels, so each regime
    level can be filtered on its own. Callers that want the aggregated
    three-level view must list the raw states explicitly, e.g.
    ``["strong", "lean_strong"]`` for the strong bucket.
    """
    if not regime_filter:
        return None
    allowed_states = {
        str(state)
        for state in (regime_filter.get("states") or [])
    }
    min_score = regime_filter.get("min_score")
    if not allowed_states and min_score is None:
        return None

    aligned = align_regime_t_minus_one(
        labels,
        regime_by_date,
        required_start,
        required_end,
    )
    mask = np.ones(len(labels), dtype=bool)
    for index, point in enumerate(aligned):
        if point is None:
            continue
        state, score = point
        mask[index] = (
            (not allowed_states or state in allowed_states)
            and (min_score is None or score >= float(min_score))
        )
    return mask
