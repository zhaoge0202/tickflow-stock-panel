"""NDJSON 流式响应的心跳保活包装。

根因: AI 分析/复盘类端点先 yield meta 再等 LLM 首包, 推理模型思考期间
流上零字节可达数分钟, 反向代理与浏览器代理会按空闲超时切断连接, 前端
fetch 抛出裸 network error。空闲期插入 {"type":"ping"} 心跳行保活;
前端各消费方与 daily_pipeline 的 recap 消费循环对未知事件类型均静默
忽略, 新增类型无兼容影响。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

# 与 api/mining.py 的 SSE 心跳间隔保持一致
HEARTBEAT_INTERVAL_SECONDS = 15.0

_HEARTBEAT_LINE = json.dumps({"type": "ping"})


async def with_heartbeat(
    events: AsyncIterator[str],
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """包装 NDJSON 事件生成器: 源空闲超过 interval 秒时插入一行心跳。

    源生成器的 yield 与异常均原样透传; 消费端停止(客户端断连)时取消
    后台泵任务, 不遗留挂起协程。
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in events:
                queue.put_nowait(("item", item))
            queue.put_nowait(("eof", None))
        except Exception as e:  # 由消费端原样重抛
            queue.put_nowait(("error", e))

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield _HEARTBEAT_LINE
                continue
            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload
            else:
                return
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
