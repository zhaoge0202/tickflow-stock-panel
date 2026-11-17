"""每个策略独立推进, 只保留最新行情, 不排队重放陈旧快照。"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from app.strategy.monitor import MonitorRuleEngine

logger = logging.getLogger(__name__)


@dataclass
class _Task:
    frame: pl.DataFrame
    token: tuple
    rules: list[dict]
    epoch: int


@dataclass
class _Lane:
    asset_type: str
    strategy_id: str
    cache_key: str
    thread: threading.Thread | None = None
    pending: _Task | None = None
    running: bool = False
    retire: bool = False
    last_elapsed_ms: float | None = None
    data_age_s: float | None = None
    last_error: str | None = None
    thread_generation: int = 0


class StrategyMonitorWorkers:
    """复用监控引擎语义, 每 (资产, 策略) 一条持久线程。

    start/stop 幂等。stop 失效正在执行的任务并清空待办; 计算无法强制中断,
    重启时复用尚未退出的线程, 防止同组重入。callback 在成功合并结果后调用,
    即使事件为空也调用, 由宿主统一发布缓存变更和通知。
    """

    def __init__(self, engine: MonitorRuleEngine, callback: Callable[[list[dict]], None]):
        self._engine = engine
        self._callback = callback
        self._condition = threading.Condition()
        self._active = False
        self._epoch = 0
        self._lanes: dict[tuple[str, str], _Lane] = {}

    @property
    def active(self) -> bool:
        with self._condition:
            return self._active

    def start(self) -> None:
        with self._condition:
            if not self._active:
                self._epoch += 1
                self._active = True
                self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._active = False
            self._epoch += 1
            for lane in self._lanes.values():
                lane.pending = None
            self._condition.notify_all()

    def submit(self, df: pl.DataFrame, asset_type: str = "stock") -> None:
        if df.is_empty():
            return
        groups = self._engine.strategy_worker_groups(asset_type)
        with self._condition:
            if not self._active:
                return
            for (asset, sid), lane in self._lanes.items():
                if asset == asset_type and sid not in groups:
                    lane.pending = None
                    lane.retire = True
            for sid, (token, rules) in groups.items():
                key = (asset_type, sid)
                lane = self._lanes.get(key)
                if lane is None:
                    # 独立可变缓冲, 不让快策略的下一轮覆写慢策略正在读的最后一行。
                    lane = _Lane(asset_type, sid, f"monitor:{asset_type}:{sid}:{id(self)}")
                    self._lanes[key] = lane
                lane.retire = False
                lane.pending = _Task(df, token, rules, self._epoch)
                if lane.thread is None or not lane.thread.is_alive():
                    lane.thread_generation += 1
                    lane.thread = threading.Thread(
                        target=self._run, args=(lane, f"{lane.cache_key}:{lane.thread_generation}"), daemon=True,
                        name=f"strategy-monitor-{asset_type}-{sid}",
                    )
                    lane.thread.start()
            self._condition.notify_all()

    def status(self) -> list[dict]:
        with self._condition:
            return [
                {
                    "asset_type": lane.asset_type, "strategy_id": lane.strategy_id,
                    "running": lane.running, "pending": lane.pending is not None,
                    "last_elapsed_ms": lane.last_elapsed_ms, "data_age_s": lane.data_age_s,
                    "last_error": lane.last_error,
                }
                for lane in self._lanes.values()
                if lane.thread is not None and lane.thread.is_alive()
            ]

    def _run(self, lane: _Lane, cache_key: str) -> None:
        used_engines: dict[int, object] = {}
        try:
            self._run_lane(lane, cache_key, used_engines)
        finally:
            # 各线程世代的 key 不同, 旧线程清理不会删掉重启后的新缓冲。
            for engine in used_engines.values():
                release = getattr(engine, "release_realtime_matrix", None)
                if callable(release):
                    try:
                        release(cache_key)
                    except Exception:
                        logger.exception("独立策略矩阵释放失败: %s", lane.strategy_id)

    def _run_lane(self, lane: _Lane, cache_key: str, used_engines: dict[int, object]) -> None:
        child: MonitorRuleEngine | None = None
        child_token: tuple | None = None
        while True:
            with self._condition:
                while lane.pending is None and self._active and not lane.retire:
                    self._condition.wait()
                if lane.pending is None or not self._active or lane.retire:
                    # 在同一锁内撤销引用; 重启只能在旧计算结束后新建同组线程。
                    lane.thread = None
                    lane.running = False
                    return
                task, lane.pending = lane.pending, None
                lane.running = True
            started = time.perf_counter()
            events: list[dict] = []
            error = None
            try:
                if child is None or task.token != child_token:
                    child = self._engine.create_strategy_worker(task.rules, cache_key)
                    used_engines[id(child._strategy_engine)] = child._strategy_engine
                    child_token = task.token
                events = child.evaluate(task.frame, asset_type=lane.asset_type)
                with self._condition:
                    accepted = (
                        self._active and task.epoch == self._epoch and not lane.retire
                        and self._engine.commit_strategy_worker(
                            lane.asset_type, lane.strategy_id, task.token, child,
                        )
                    )
                if accepted:
                    # 不持管理锁或引擎锁调用宿主, 避免慢通知拖住其他策略。
                    self._callback(events)
                else:
                    child = None  # 被丢弃的去重状态不得污染下一次有效评估。
                    child_token = None
            except Exception as exc:  # 一组失败不得影响其他策略
                error = str(exc)
                child = None
                child_token = None
                logger.exception("独立策略监控失败: %s (%s)", lane.strategy_id, lane.asset_type)
            elapsed_ms = (time.perf_counter() - started) * 1000
            age_s = None
            if "quote_ts" in task.frame.columns:
                try:
                    newest = task.frame["quote_ts"].max()
                    if newest is not None:
                        age_s = max(0.0, time.time() - float(newest) / 1000.0)
                except (TypeError, ValueError):
                    pass
            with self._condition:
                lane.running = False
                lane.last_elapsed_ms = elapsed_ms
                lane.data_age_s = age_s
                lane.last_error = error
            logger.info(
                "独立策略监控 %s (%s) %.1fs, 数据龄=%s, 事件=%d",
                lane.strategy_id, lane.asset_type, elapsed_ms / 1000,
                f"{age_s:.1f}s" if age_s is not None else "未知", len(events),
            )
