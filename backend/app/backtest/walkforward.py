"""Walk-forward 优化 — 滚动窗口的样本内优化 + 样本外验证。

每折在训练区间用参数网格优化选出最优参数, 再在紧邻的测试区间用该参数做样本外(OOS)
回测。滚动前移。核心产出是 OOS 拼接净值 + 每折 IS-vs-OOS 退化 —— 样本内漂亮、样本外
崩溃即过拟合信号, 单次样本内回测看不到。

依赖 PR2a 的 StrategyOptimizer 做每折训练区间的网格优化。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)


@dataclass
class Fold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def generate_folds(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[Fold]:
    """滚动窗口 fold 切分: 训练窗口固定长度, 测试窗口紧接其后, 按 step 前移。

    测试区间超出 end 即停止。数据区间放不下一折则抛错。
    """
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days / test_days / step_days 必须为正")

    folds: list[Fold] = []
    i = 0
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(Fold(i, train_start, train_end, test_start, test_end))
        i += 1
        train_start = train_start + timedelta(days=step_days)

    if not folds:
        raise ValueError(
            f"数据区间不足以切出至少一折 (需 train+test={train_days + test_days}天, "
            f"实有 {(end - start).days}天)"
        )
    return folds


def aggregate_oos(fold_records: list[dict], objective: str) -> dict:
    """从各折 OOS 结果聚合: 复利净值曲线 / IS-OOS 退化 / 一致性。

    fold_records: [{index, test_end, best_params, is_score, oos_stats}]
    - compounded_oos_return: 各折 OOS 总收益复利
    - avg_is_objective / avg_oos_objective / degradation: IS 目标均值 - OOS 目标均值,
      正值 = 样本外退化 = 过拟合信号
    - consistency: OOS 目标 > 0 的折占比
    """
    n = len(fold_records)
    if n == 0:
        return {
            "n_folds": 0,
            "compounded_oos_return": 0.0,
            "avg_is_objective": None,
            "avg_oos_objective": None,
            "degradation": None,
            "consistency": 0.0,
            "oos_equity_curve": [],
        }

    equity = 1.0
    curve: list[dict] = []
    for f in fold_records:
        r = float(f["oos_stats"].get("total_return", 0.0) or 0.0)
        equity *= (1 + r)
        curve.append({"fold": f["index"], "date": str(f["test_end"]), "value": round(equity, 4)})

    is_vals = [f["is_score"] for f in fold_records if f["is_score"] is not None]
    oos_vals = [f["oos_stats"].get(objective) for f in fold_records]
    oos_vals = [v for v in oos_vals if v is not None]

    avg_is = round(float(sum(is_vals) / len(is_vals)), 4) if is_vals else None
    avg_oos = round(float(sum(oos_vals) / len(oos_vals)), 4) if oos_vals else None
    degradation = round(avg_is - avg_oos, 4) if (avg_is is not None and avg_oos is not None) else None
    n_positive = sum(1 for v in oos_vals if v > 0)
    consistency = round(n_positive / len(oos_vals), 4) if oos_vals else 0.0

    return {
        "n_folds": n,
        "compounded_oos_return": round(equity - 1.0, 4),
        "avg_is_objective": avg_is,
        "avg_oos_objective": avg_oos,
        "degradation": degradation,
        "consistency": consistency,
        "oos_equity_curve": curve,
    }


@dataclass
class WalkForwardConfig:
    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    param_grid: dict
    objective: str = "sortino"
    train_days: int = 252
    test_days: int = 63
    step_days: int = 63
    direction: str | None = None
    max_workers: int = 4
    base_params: dict = field(default_factory=dict)
    overrides: dict | None = None
    backtest_kwargs: dict = field(default_factory=dict)


class WalkForwardService:
    """滚动窗口 walk-forward: 每折训练区间优化 -> 测试区间 OOS 验证 -> 聚合。"""

    def __init__(self, optimizer, service, strategy_engine) -> None:
        self.optimizer = optimizer
        self.service = service
        self.strategy_engine = strategy_engine

    def run(
        self,
        cfg: WalkForwardConfig,
        progress_cb=None,
        cancel_event=None,
    ) -> dict:
        from app.backtest.optimizer import OptimizeConfig
        from app.backtest.strategy import StrategyBacktestConfig

        t0 = time.perf_counter()
        folds = generate_folds(cfg.start, cfg.end, cfg.train_days, cfg.test_days, cfg.step_days)
        n_total = len(folds)

        fold_records: list[dict] = []
        for f in folds:
            if cancel_event is not None and cancel_event.is_set():
                break

            # 训练区间: 网格优化选最优参数
            opt_cfg = OptimizeConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.train_start,
                end=f.train_end,
                param_grid=cfg.param_grid,
                objective=cfg.objective,
                direction=cfg.direction,
                max_workers=cfg.max_workers,
                base_params=cfg.base_params,
                overrides=cfg.overrides,
                backtest_kwargs=cfg.backtest_kwargs,
            )
            opt_res = self.optimizer.optimize(opt_cfg, cancel_event=cancel_event)
            best_params = opt_res.get("best_params")
            is_score = opt_res.get("best_score")

            # 测试区间: 用最优参数做样本外回测
            merged = {**cfg.base_params, **(best_params or {})}
            oos_cfg = StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.test_start,
                end=f.test_end,
                params=merged,
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            oos_res = self.service.run(oos_cfg, cancel_event=cancel_event)
            oos_stats = {} if oos_res.error else oos_res.stats

            fold_records.append({
                "index": f.index,
                "train_start": str(f.train_start),
                "train_end": str(f.train_end),
                "test_start": str(f.test_start),
                "test_end": str(f.test_end),
                "best_params": best_params,
                "is_score": is_score,
                "oos_objective": oos_stats.get(cfg.objective),
                "oos_stats": oos_stats,
            })

            if progress_cb is not None:
                progress_cb({
                    "type": "walkforward_progress",
                    "done": len(fold_records),
                    "total": n_total,
                    "fold": f.index,
                })

        summary = aggregate_oos(fold_records, cfg.objective)
        return {
            "objective": cfg.objective,
            "n_folds": len(fold_records),
            "n_planned_folds": n_total,
            "folds": fold_records,
            "summary": summary,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
