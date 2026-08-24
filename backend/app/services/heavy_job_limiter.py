"""Weighted process-local limiter for memory-heavy jobs."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Literal

HeavyJobKind = Literal["normal", "mining"]


class HeavyJobLimitTimeoutError(TimeoutError):
    """Raised when a heavy-job slot cannot be acquired before its deadline."""


class HeavyJobCancelledError(RuntimeError):
    """Raised when slot acquisition is cancelled while waiting."""


class HeavyJobLimiter:
    """A weighted limiter where normal jobs cost one slot and mining costs two."""

    _WEIGHTS: ClassVar[dict[HeavyJobKind, int]] = {"normal": 1, "mining": 2}

    def __init__(self, capacity: int = 2, *, cancel_poll_interval: float = 0.05) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if cancel_poll_interval <= 0:
            raise ValueError("cancel_poll_interval must be positive")
        self.capacity = capacity
        self._cancel_poll_interval = cancel_poll_interval
        self._used = 0
        self._acquired = {"normal": 0, "mining": 0}
        self._condition = threading.Condition()

    @property
    def in_use(self) -> int:
        with self._condition:
            return self._used

    @property
    def available(self) -> int:
        with self._condition:
            return self.capacity - self._used

    def acquire(
        self,
        kind: HeavyJobKind = "normal",
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Wait for capacity and return ``False`` on cancellation or timeout."""
        weight = self._weight(kind)
        if weight > self.capacity:
            raise ValueError(f"{kind} requires {weight} slots, capacity is {self.capacity}")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                if self._used + weight <= self.capacity:
                    self._used += weight
                    self._acquired[kind] += 1
                    return True

                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                wait_for = remaining
                if cancel_event is not None:
                    wait_for = self._cancel_poll_interval
                    if remaining is not None:
                        wait_for = min(wait_for, remaining)
                self._condition.wait(wait_for)

    def release(self, kind: HeavyJobKind = "normal") -> None:
        """Return capacity previously acquired for ``kind``."""
        weight = self._weight(kind)
        with self._condition:
            if self._acquired[kind] == 0:
                raise RuntimeError(f"cannot release unacquired {kind} capacity")
            self._acquired[kind] -= 1
            self._used -= weight
            self._condition.notify_all()

    @contextmanager
    def slot(
        self,
        kind: HeavyJobKind = "normal",
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[HeavyJobLimiter]:
        """Acquire weighted capacity for the duration of a ``with`` block."""
        acquired = self.acquire(kind, timeout=timeout, cancel_event=cancel_event)
        if not acquired:
            if cancel_event is not None and cancel_event.is_set():
                raise HeavyJobCancelledError(f"{kind} job was cancelled while waiting")
            raise HeavyJobLimitTimeoutError(f"timed out waiting for {kind} job capacity")
        try:
            yield self
        finally:
            self.release(kind)

    @classmethod
    def _weight(cls, kind: HeavyJobKind) -> int:
        try:
            return cls._WEIGHTS[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported heavy job kind: {kind!r}") from exc


shared_heavy_job_limiter = HeavyJobLimiter(capacity=2)
# Short alias for entry points that prefer the existing module-singleton naming style.
heavy_job_limiter = shared_heavy_job_limiter
