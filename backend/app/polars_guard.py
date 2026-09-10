"""polars collect 并发闸。

polars 的共享执行器 (rayon 工作池 + 流式引擎异步运行时) 在多线程并发 collect
时存在死锁问题: 上游 issue #24448 / #23053 / #25754 等同族案例均为「在飞的
collect 超过池内工作位 → 持有工作位的任务等待排不上队的任务 → 0 CPU 永久
挂起」。本模块用进程级信号量限制同时在飞的 collect 数量 —— 这是上游 issue
区被反复验证有效的缓解手段。

车道设计: 总闸位 polars_collect_permits 个, 其中 background (预热 / 增量 /
维表加载等后台计算) 最多占 polars_collect_background_permits 个, 其余闸位
保留给 interactive (页面读接口), 保证后台大计算不会把页面请求饿死。获取顺序
恒为 background 车道 → 总闸, 不存在环。

worker 子进程 (回测/优化/挖掘) 单任务串行执行, 不经过本闸。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import polars as pl

from app.config import settings

CollectPriority = Literal["interactive", "background"]

_TOTAL_GATE = threading.BoundedSemaphore(settings.polars_collect_permits)
_BACKGROUND_LANE = threading.BoundedSemaphore(settings.polars_collect_background_permits)


@contextmanager
def collect_slot(priority: CollectPriority = "interactive") -> Iterator[None]:
    """占用一个 collect 闸位; background 需同时占用车道位与总闸位。"""
    if priority == "background":
        with _BACKGROUND_LANE, _TOTAL_GATE:
            yield
        return
    with _TOTAL_GATE:
        yield


def guarded_collect(
    lf: pl.LazyFrame,
    *,
    priority: CollectPriority = "interactive",
    **kwargs: object,
) -> pl.DataFrame:
    """在并发闸内执行 LazyFrame.collect; 语义不变, 仅串行化调度。"""
    with collect_slot(priority):
        return lf.collect(**kwargs)
