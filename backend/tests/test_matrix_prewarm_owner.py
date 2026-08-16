from __future__ import annotations

import threading
import time

from app.services.heavy_job_limiter import HeavyJobCancelledError, HeavyJobLimiter
from app.services.matrix_prewarm_owner import MatrixCachePrewarmOwner


def test_owner_deduplicates_running_work_and_reuses_after_completion() -> None:
    owner = MatrixCachePrewarmOwner()
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def target() -> None:
        calls.append("run")
        started.set()
        assert release.wait(2)

    assert owner.schedule(target)
    assert started.wait(1)
    assert not owner.schedule(target)

    release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and len(calls) < 2:
        if owner.schedule(lambda: calls.append("run")):
            break
        time.sleep(0.005)

    assert calls == ["run", "run"]
    assert owner.shutdown(timeout=1)


def test_shutdown_cancels_limiter_wait_and_rejects_new_work() -> None:
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.005)
    owner = MatrixCachePrewarmOwner()
    waiting = threading.Event()
    cancelled = threading.Event()
    assert limiter.acquire("mining", timeout=0)

    def target() -> None:
        waiting.set()
        try:
            with limiter.slot("normal", cancel_event=owner.cancel_event):
                raise AssertionError("cancelled prewarm must not acquire capacity")
        except HeavyJobCancelledError:
            cancelled.set()

    assert owner.schedule(target)
    assert waiting.wait(1)
    assert owner.shutdown(timeout=1)
    assert cancelled.is_set()
    assert not owner.schedule(target)
    limiter.release("mining")


def test_shutdown_join_is_bounded_for_uncooperative_target() -> None:
    owner = MatrixCachePrewarmOwner()
    release = threading.Event()
    started = threading.Event()

    def target() -> None:
        started.set()
        release.wait(2)

    assert owner.schedule(target)
    assert started.wait(1)
    before = time.monotonic()
    assert not owner.shutdown(timeout=0.02)
    assert time.monotonic() - before < 0.2

    release.set()
    assert owner.shutdown(timeout=1)
