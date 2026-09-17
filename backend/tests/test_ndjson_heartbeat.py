"""NDJSON 心跳包装与 LLM 客户端重试配置的测试。

背景: AI 分析/复盘端点等 LLM 首包期间流上零字节, 会被代理按空闲超时
切断连接(前端报裸 network error)。with_heartbeat 在空闲期插入
{"type":"ping"} 行保活; _openai_client 的 max_retries 让首包前失败可重试。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import secrets_store
from app.services import ai_provider
from app.services.ndjson_heartbeat import with_heartbeat


async def _collect(gen) -> list[dict]:
    out = []
    async for line in gen:
        out.append(json.loads(line))
    return out


@pytest.mark.asyncio
async def test_with_heartbeat_inserts_ping_only_when_idle():
    async def slow_source():
        yield json.dumps({"type": "meta"})
        await asyncio.sleep(0.15)  # 空闲窗口, 应插 ping
        yield json.dumps({"type": "delta", "content": "x"})
        yield json.dumps({"type": "done"})

    events = await _collect(with_heartbeat(slow_source(), interval=0.05))
    types = [e["type"] for e in events]

    assert types[0] == "meta"
    assert types[-2] == "delta"
    assert types[-1] == "done"
    # 中间全部是心跳, 数据完整且相对顺序不变
    assert types[1:-2] and all(t == "ping" for t in types[1:-2])
    assert events[-2]["content"] == "x"


@pytest.mark.asyncio
async def test_with_heartbeat_no_ping_when_source_active():
    async def fast_source():
        for i in range(5):
            yield json.dumps({"type": "delta", "content": str(i)})

    events = await _collect(with_heartbeat(fast_source(), interval=30.0))
    assert [e["type"] for e in events] == ["delta"] * 5
    assert [e["content"] for e in events] == [str(i) for i in range(5)]


@pytest.mark.asyncio
async def test_with_heartbeat_propagates_source_error():
    async def bad_source():
        yield json.dumps({"type": "meta"})
        raise RuntimeError("boom")

    lines: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        async for line in with_heartbeat(bad_source(), interval=0.05):
            lines.append(line)
    assert len(lines) == 1  # 异常前已收到的数据不丢


@pytest.mark.asyncio
async def test_with_heartbeat_cancels_pump_on_client_disconnect():
    async def endless_source():
        while True:
            await asyncio.sleep(0.01)
            yield json.dumps({"type": "delta", "content": "tick"})

    gen = with_heartbeat(endless_source(), interval=0.05)
    first = await gen.__anext__()
    assert json.loads(first)["type"] == "delta"
    await gen.aclose()  # 模拟客户端断连; pump 应被取消且不挂起


def test_openai_client_enables_bounded_retries(monkeypatch):
    monkeypatch.setattr(
        secrets_store,
        "get_ai_config",
        lambda key, default="": "https://api.deepseek.com/v1" if key == "ai_base_url" else "",
    )
    client = ai_provider._openai_client("test-key", 30.0)
    assert client.max_retries == 2
