"""自动挖掘 L1 编排: 全量因子统计筛选 → 达标池。

流程定位 (对应方案「四层漏斗」):
- L1 本模块: 注册表全量因子批量检验, 按置信档门槛筛出达标因子 (近期窗口,
  仅作"有信号"的先验过滤; 最终达标由挖掘引擎的逐折训练选择与嵌套样本外
  验证裁定)。
- L2/L3/L4 由现有挖掘引擎完成: 相关性剪枝 (prune_correlated_factors)、
  束搜索组合 (beam_search_factor_combinations)、嵌套样本外验证与达标
  门槛 (evaluate_candidate_gate), 本模块不重复实现。

达标判据与检验页服务端判读同源 (|t_NW| / BH q / |IC| / |IR|), 按档放宽或收紧;
q 值缺失时按"通过"处理 (探索档小样本下 BH 校正保守)。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from app.backtest.factor import FactorBacktestService, FactorBatchConfig
from app.factors.registry import factor_columns_view

Profile = Literal["exploratory", "balanced", "strict"]

# 挖掘请求的因子池上限 (与 MiningStartRequest.factor_names max_length 对齐)
MAX_AUTO_POOL = 48

# L1 筛选窗口: 近一年 (与挖掘窗口解耦, 只筛"近期有信号", 长窗口验证交给引擎)
SCREEN_WINDOW_DAYS = 365


@dataclass(frozen=True)
class ScreenGate:
    min_abs_ic: float
    min_abs_ir: float
    min_abs_t: float
    max_q: float

    def to_dict(self) -> dict[str, float]:
        return {
            "min_abs_ic": self.min_abs_ic,
            "min_abs_ir": self.min_abs_ir,
            "min_abs_t": self.min_abs_t,
            "max_q": self.max_q,
        }


SCREEN_GATES: dict[str, ScreenGate] = {
    "exploratory": ScreenGate(min_abs_ic=0.02, min_abs_ir=0.15, min_abs_t=1.5, max_q=0.20),
    "balanced": ScreenGate(min_abs_ic=0.02, min_abs_ir=0.30, min_abs_t=2.0, max_q=0.10),
    "strict": ScreenGate(min_abs_ic=0.03, min_abs_ir=0.50, min_abs_t=2.5, max_q=0.05),
}


def classify_factor(item: dict[str, Any], gate: ScreenGate) -> str | None:
    """返回 None 表示达标; 否则返回首个未过的门槛, 格式统一为「类别 (细节)」。"""
    if item.get("error"):
        return f"计算失败 ({str(item['error'])[:40]})"
    ic = item.get("ic_mean")
    ir = item.get("ir")
    t = item.get("t_newey_west")
    q = item.get("q_value")
    if ic is None or ir is None:
        return "样本不足 (无有效 IC/IR)"
    if abs(ic) < gate.min_abs_ic:
        return f"预测力弱 (|IC|<{gate.min_abs_ic:.2f})"
    if abs(ir) < gate.min_abs_ir:
        return f"稳定度低 (|IR|<{gate.min_abs_ir:.2f})"
    if t is None:
        return "样本不足 (无 NW t 值)"
    if abs(t) < gate.min_abs_t:
        return f"不显著 (|t|<{gate.min_abs_t:.1f})"
    if q is not None and q > gate.max_q:
        return f"多重检验未过 (q>{gate.max_q:.2f})"
    return None


def _short_reason(reason: str) -> str:
    """失败原因归并到短类别 (「类别 (细节)」的前半段), 供原因分布统计。"""
    return reason.split(" (", 1)[0].strip()


def _finite_or_none(value: Any) -> float | None:
    """NaN/Inf 一律归 None, 避免写入任务存储时产生非法 JSON。"""
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _metric_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "factor_name": item.get("factor_name"),
        "label": item.get("label") or item.get("factor_name"),
        "group": item.get("group") or "",
        "ic": _finite_or_none(item.get("ic_mean")),
        "ir": _finite_or_none(item.get("ir")),
        "t": _finite_or_none(item.get("t_newey_west")),
        "q": _finite_or_none(item.get("q_value")),
        "direction": 1 if (item.get("ic_mean") or 0) >= 0 else -1,
    }


def screen_all_factors(
    engine: Any,
    *,
    asset_type: str,
    start: date | None,
    end: date,
    profile: str,
    max_factors: int = MAX_AUTO_POOL,
) -> dict[str, Any]:
    """L1 全量筛选: 注册表全部适用因子批量检验 → 达标池 + 失败原因分布。

    start=None 时取近 SCREEN_WINDOW_DAYS 天; 显式 start 只会收紧 (不放宽) 筛选窗口。
    """
    gate = SCREEN_GATES.get(profile)
    if gate is None:
        raise ValueError(f"unknown mining profile: {profile}")

    candidates = [
        str(item["id"])
        for item in factor_columns_view()
        if asset_type in item.get("asset_types", ["stock"])
    ]
    screen_start = max(start or date.min, end - timedelta(days=SCREEN_WINDOW_DAYS))
    began = time.perf_counter()
    service = FactorBacktestService(engine)
    batch = service.run_batch(FactorBatchConfig(
        factor_names=candidates,
        symbols=None,
        start=screen_start,
        end=end,
        rebalance="daily",
        asset_type=asset_type,
    ))
    elapsed_ms = round((time.perf_counter() - began) * 1000, 1)

    qualified: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    by_name = {str(getattr(item, "factor_name", None)): item for item in batch.results}
    for name in candidates:
        item = by_name.get(name)
        if item is None:
            failed.append({"factor_name": name, "label": name, "group": "",
                           "ic": None, "ir": None, "t": None, "q": None,
                           "reason": "未返回结果"})
            continue
        # 非有限值先清洗 (NaN 与任何比较均为 False, 会绕过门槛误判达标)
        reason = classify_factor({
            "error": getattr(item, "error", None),
            "ic_mean": _finite_or_none(getattr(item, "ic_mean", None)),
            "ir": _finite_or_none(getattr(item, "ir", None)),
            "t_newey_west": _finite_or_none(getattr(item, "t_newey_west", None)),
            "q_value": _finite_or_none(getattr(item, "q_value", None)),
        }, gate)
        row = _metric_row({
            "factor_name": getattr(item, "factor_name", None),
            "label": getattr(item, "label", None),
            "group": getattr(item, "group", None),
            "ic_mean": getattr(item, "ic_mean", None),
            "ir": getattr(item, "ir", None),
            "t_newey_west": getattr(item, "t_newey_west", None),
            "q_value": getattr(item, "q_value", None),
        })
        if reason is None:
            qualified.append(row)
        else:
            failed.append({**row, "reason": reason})

    # 池按 |IC|*|IR| 降序 (截面信噪比口径), 截断到挖掘上限
    qualified.sort(key=lambda row: abs(row["ic"] or 0.0) * abs(row["ir"] or 0.0), reverse=True)
    pool = [row["factor_name"] for row in qualified[:max_factors]]

    reason_counts: dict[str, int] = {}
    for row in failed:
        category = _short_reason(row["reason"])
        reason_counts[category] = reason_counts.get(category, 0) + 1

    return {
        "profile": profile,
        "gate": gate.to_dict(),
        "screen_window": {"start": screen_start.isoformat(), "end": end.isoformat()},
        "n_total": len(candidates),
        "n_qualified": len(qualified),
        "pool": pool,
        "pool_truncated": len(qualified) > len(pool),
        "qualified": qualified,
        "failed": failed,
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])),
        "elapsed_ms": elapsed_ms,
    }
