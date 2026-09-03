"""#203 回归: PullScheduler.refresh 的 _tasks 增删 diff 全部在主循环闭包内完成。

refresh 可能从 FastAPI worker 线程调用; 旧实现先在调用方线程读 _tasks
做 diff 再提交闭包, 两步之间主循环可能已改动字典 (TOCTOU)。
本测试验证行为正确性 (增/删/幂等); 竞态本身无法确定性复现。
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.services import ext_pull
from app.services.ext_data import ExtConfig, PullConfig
from app.services.ext_pull import PullScheduler


def _cfg(cid: str, enabled: bool = True) -> ExtConfig:
    return ExtConfig(
        id=cid, label=cid, mode="snapshot", fields=[],
        pull=PullConfig(
            enabled=enabled, url=f"https://example.test/{cid}",
            schedule_minutes=60,
        ) if enabled else None,
    )


@pytest.fixture()
def scheduler(monkeypatch, tmp_path):
    """跑在真实事件循环上的调度器; _run_loop 打桩避免真实网络。"""
    loop = asyncio.new_event_loop()
    s = PullScheduler()
    s._loop = loop
    s._running = True

    async def _idle(self, config):  # 打桩: 挂起不退出, 等 cancel (self 因类级打桩传入)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    monkeypatch.setattr(PullScheduler, "_run_loop", _idle)
    configs: list[ExtConfig] = []

    def _load_all(self):
        return list(configs)

    monkeypatch.setattr(ext_pull.ExtConfigStore, "load_all", _load_all)
    yield s, loop, configs
    for t in list(s._tasks.values()):
        t.cancel()
    loop.run_until_complete(asyncio.sleep(0))  # 让取消送达协程
    loop.close()


def _drain(loop):
    loop.run_until_complete(asyncio.sleep(0))


def test_refresh_schedules_enabled_configs(scheduler, tmp_path) -> None:
    s, loop, configs = scheduler
    configs.extend([_cfg("a"), _cfg("b"), _cfg("c", enabled=False)])

    s.refresh(tmp_path)  # data_dir 仅透传, 配置来自打桩的 load_all
    _drain(loop)
    assert set(s._tasks) == {"a", "b"}

    # 幂等: 重复 refresh 不重复建任务
    s.refresh(tmp_path)
    _drain(loop)
    assert set(s._tasks) == {"a", "b"}


def test_refresh_removes_disabled_configs(scheduler, tmp_path) -> None:
    s, loop, configs = scheduler
    configs.extend([_cfg("a"), _cfg("b")])
    s.refresh(tmp_path)
    _drain(loop)
    assert set(s._tasks) == {"a", "b"}

    # b 被禁用 → 下次 refresh 移除其任务
    configs[:] = [_cfg("a")]
    s.refresh(tmp_path)
    _drain(loop)
    assert set(s._tasks) == {"a"}
