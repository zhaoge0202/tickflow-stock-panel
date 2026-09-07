"""看门狗触发逻辑测试 (不真退出进程)。"""
from __future__ import annotations

import asyncio

from app.watchdog import HealthWatchdog, default_probe


async def _run_watchdog(probe_results, *, threshold=2, interval=0.01, timeout=0.2):
    exits: list[int] = []
    idx = 0

    def probe() -> None:
        nonlocal idx
        if idx < len(probe_results):
            result = probe_results[idx]
            idx += 1
            if isinstance(result, BaseException):
                raise result
        # 脚本耗尽后恒为成功

    wd = HealthWatchdog(
        probe,
        exit_cb=exits.append,
        interval_s=interval,
        probe_timeout_s=timeout,
        failure_threshold=threshold,
    )
    wd.start()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if exits or wd._task.done():
            break
    await wd.stop()
    return exits


async def test_consecutive_failures_trigger_exit() -> None:
    exits = await _run_watchdog([RuntimeError("wedge"), TimeoutError("wedge")])
    assert exits == [70]


async def test_success_resets_failure_counter() -> None:
    # 失败 1 次 → 成功 → 再失败 1 次: 未达连续阈值, 不退出。
    exits = await _run_watchdog([RuntimeError("slow"), None, RuntimeError("slow")])
    assert exits == []


async def test_default_probe_passes_on_healthy_resources() -> None:
    import threading

    lock = threading.Lock()
    default_probe(lock)  # 不抛即通过
    default_probe(None)
