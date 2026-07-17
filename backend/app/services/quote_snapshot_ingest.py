"""最新行情快照的有界异步 MySQL 写入器。

只保留待写入的最后一批行情。数据库变慢时丢弃过期中间批次，不能让
实时行情线程和进程内队列无限堆积。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from app.services.quote_snapshot_mysql import quote_snapshot_mysql_store

logger = logging.getLogger(__name__)


class QuoteSnapshotIngestor:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: list[dict[str, Any]] | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not quote_snapshot_mysql_store.enabled() or self._running:
            return
        with self._condition:
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                name="quote-snapshot-mysql",
                daemon=True,
            )
            self._thread.start()
        logger.info("quote snapshot MySQL writer started")

    def stop(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("quote snapshot MySQL writer stopped")

    def submit(self, records: list[dict[str, Any]]) -> bool:
        if not records or not quote_snapshot_mysql_store.enabled():
            return False
        with self._condition:
            if not self._running:
                return False
            # 只保留最新一批，避免数据库异常时积压行情快照。
            self._pending = records
            self._condition.notify()
        return True

    def _run_loop(self) -> None:
        while True:
            with self._condition:
                while self._running and self._pending is None:
                    self._condition.wait()
                if not self._running and self._pending is None:
                    return
                records = self._pending
                self._pending = None

            if not records:
                continue
            try:
                written = quote_snapshot_mysql_store.upsert(records)
                logger.info("最新行情快照已写入 MySQL: %d 只", written)
            except Exception as exc:  # noqa: BLE001
                logger.warning("最新行情快照写入 MySQL 失败, 已降级到本地存储: %s", exc)


quote_snapshot_ingestor = QuoteSnapshotIngestor()
