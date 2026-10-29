"""行情轮询间隔语义回归: 间隔是「周期」, 不是「空闲时长」.

回归点: 旧实现本轮耗时 > interval 时把下轮等待改成 `本轮耗时 × 1.2`,
3s 间隔在单轮 30s 的机器上被放大成 36s, 实际周期 66s —— 用户设置的间隔
完全失效, 且没有任何恢复路径。
"""
from __future__ import annotations

from app.services.quote_service import QuoteService


def _service(interval: float) -> QuoteService:
    # 不跑 __init__: 它会读 preferences 并起 webhook 线程池, 与本测试无关。
    svc = QuoteService.__new__(QuoteService)
    svc._interval = interval
    return svc


def test_sleep_subtracts_work_from_period():
    svc = _service(6.0)
    assert svc._next_sleep_s(1.5) == 4.5


def test_sleep_is_zero_when_round_exceeds_period():
    svc = _service(3.0)
    assert svc._next_sleep_s(30.0) == 0.0


def test_slow_round_does_not_inflate_next_wait():
    svc = _service(3.0)
    # 旧实现会返回 29.9 * 1.2 = 35.88s
    assert svc._next_sleep_s(29.9) == 0.0
    assert svc._next_sleep_s(0.5) == 2.5
