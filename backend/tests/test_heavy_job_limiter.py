from __future__ import annotations

import threading
import time

import pytest

from app.services.heavy_job_limiter import (
    HeavyJobCancelledError,
    HeavyJobLimiter,
    HeavyJobLimitTimeoutError,
    heavy_job_limiter,
    shared_heavy_job_limiter,
)


def test_weighted_capacity_and_timeout() -> None:
    limiter = HeavyJobLimiter(capacity=2)

    assert limiter.acquire("normal", timeout=0)
    assert limiter.acquire("normal", timeout=0)
    assert limiter.available == 0
    assert not limiter.acquire("normal", timeout=0.01)
    limiter.release("normal")
    assert not limiter.acquire("mining", timeout=0)
    limiter.release("normal")
    assert limiter.acquire("mining", timeout=0)
    assert limiter.in_use == 2
    limiter.release("mining")


def test_waiting_acquire_can_be_cancelled() -> None:
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.01)
    cancel_event = threading.Event()
    assert limiter.acquire("mining", timeout=0)

    result: list[bool] = []
    waiter = threading.Thread(
        target=lambda: result.append(
            limiter.acquire("normal", timeout=1, cancel_event=cancel_event)
        )
    )
    waiter.start()
    time.sleep(0.03)
    cancel_event.set()
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == [False]
    assert limiter.in_use == 2
    limiter.release("mining")


def test_context_manager_releases_after_body_error() -> None:
    limiter = HeavyJobLimiter(capacity=2)

    with pytest.raises(ValueError, match="body failed"), limiter.slot("mining", timeout=0):
        raise ValueError("body failed")

    assert limiter.in_use == 0
    assert limiter.acquire("mining", timeout=0)
    limiter.release("mining")


def test_context_manager_distinguishes_timeout_and_cancellation() -> None:
    limiter = HeavyJobLimiter(capacity=2)
    assert limiter.acquire("mining", timeout=0)

    with pytest.raises(HeavyJobLimitTimeoutError), limiter.slot("normal", timeout=0.01):
        pytest.fail("unreachable")

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(HeavyJobCancelledError), limiter.slot("normal", cancel_event=cancelled):
        pytest.fail("unreachable")

    limiter.release("mining")


def test_invalid_release_does_not_overfill_capacity() -> None:
    limiter = HeavyJobLimiter(capacity=2)

    with pytest.raises(RuntimeError):
        limiter.release("normal")
    assert limiter.available == 2

    assert limiter.acquire("normal", timeout=0)
    with pytest.raises(RuntimeError):
        limiter.release("mining")
    assert limiter.in_use == 1
    limiter.release("normal")


def test_module_aliases_share_the_capacity_two_singleton() -> None:
    assert heavy_job_limiter is shared_heavy_job_limiter
    assert shared_heavy_job_limiter.capacity == 2
