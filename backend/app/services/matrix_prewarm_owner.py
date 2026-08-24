from __future__ import annotations

import threading
from collections.abc import Callable


class MatrixCachePrewarmOwner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def schedule(self, target: Callable[[], None]) -> bool:
        with self._lock:
            if self._cancel_event.is_set():
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            thread = threading.Thread(
                target=self._run,
                args=(target,),
                name="matrix-cache-prewarm",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        self._cancel_event.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def _run(self, target: Callable[[], None]) -> None:
        try:
            target()
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
