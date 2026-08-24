from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from queue import Empty

from app.services.mining_process_lock import (
    MiningProcessLock,
    MiningProcessLockError,
)


def _acquire_in_spawned_process(data_dir: str, result_queue) -> None:
    lock = MiningProcessLock(Path(data_dir))
    try:
        lock.acquire()
    except MiningProcessLockError as exc:
        result_queue.put(("blocked", str(exc)))
        return
    try:
        result_queue.put(("acquired", None))
    finally:
        lock.release()


def _spawn_lock_attempt(data_dir: Path) -> tuple[str, str | None]:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_acquire_in_spawned_process,
        args=(str(data_dir), result_queue),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise AssertionError("spawned lock process did not exit")
    assert process.exitcode == 0
    try:
        return result_queue.get(timeout=2)
    except Empty as exc:
        raise AssertionError("spawned lock process returned no result") from exc
    finally:
        result_queue.close()
        result_queue.join_thread()


def test_process_lock_rejects_contention_and_allows_acquire_after_release(
    tmp_path,
) -> None:
    owner = MiningProcessLock(tmp_path)
    owner.acquire()

    blocked, message = _spawn_lock_attempt(tmp_path)
    assert blocked == "blocked"
    assert message is not None and "already owns mining" in message

    owner.release()
    acquired, message = _spawn_lock_attempt(tmp_path)
    assert acquired == "acquired"
    assert message is None


def test_process_lock_handle_is_not_inheritable_and_release_is_idempotent(
    tmp_path,
) -> None:
    lock = MiningProcessLock(tmp_path)
    lock.acquire()

    stream = vars(lock)["_stream"]
    assert stream is not None
    assert not os.get_inheritable(stream.fileno())

    lock.release()
    lock.release()
