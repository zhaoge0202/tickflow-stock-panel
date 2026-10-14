"""TDX 逐笔成交异步入库队列。"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.config import settings
from app.market_time import cn_today, is_trading_weekday
from app.plugins.tdxapi.provider import TDXAPIProvider
from app.services.trade_tick_mysql import trade_tick_mysql_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeTickIngestTask:
    symbol: str
    trade_date: date
    force: bool = False

    @property
    def key(self) -> tuple[str, date]:
        return (self.symbol, self.trade_date)


class TradeTickIngestor:
    def __init__(self) -> None:
        self._queue: queue.Queue[TradeTickIngestTask] = queue.Queue(maxsize=256)
        self._pending: set[tuple[str, date]] = set()
        self._last_saved: dict[tuple[str, date], float] = {}
        self._status: dict[tuple[str, date], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="trade-tick-ingest", daemon=True)
        self._thread.start()
        logger.info("trade tick ingestor started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def enqueue(
        self,
        symbol: str,
        trade_date: date | str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return {"status": "rejected", "message": "symbol 不能为空"}
        day = _parse_date(trade_date) or cn_today()
        # 非交易日没有真实逐笔成交: 数据源返回的是上一交易日快照, 按当天日期
        # 入库会产生日期错误的假日逐笔 (如 2026-08-30 复制 08-28)。
        if not is_trading_weekday(day):
            return {
                "status": "rejected",
                "symbol": symbol,
                "date": day.isoformat(),
                "message": "非交易日无逐笔成交, 跳过入库",
            }

        if not trade_tick_mysql_store.enabled():
            return {
                "status": "disabled",
                "symbol": symbol,
                "date": day.isoformat(),
                "message": "逐笔成交 MySQL 持久化未启用或未配置",
            }

        task = TradeTickIngestTask(symbol=symbol, trade_date=day, force=force)
        with self._lock:
            if task.key in self._pending:
                return {"status": "queued", "symbol": symbol, "date": day.isoformat(), "coalesced": True}
            self._pending.add(task.key)
            self._status[task.key] = {
                "status": "queued",
                "symbol": symbol,
                "date": day.isoformat(),
                "queued_at": _now_iso(),
                "error": None,
            }
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            with self._lock:
                self._pending.discard(task.key)
                self._status[task.key] = {
                    "status": "rejected",
                    "symbol": symbol,
                    "date": day.isoformat(),
                    "error": "入库队列已满",
                    "finished_at": _now_iso(),
                }
            return {"status": "rejected", "symbol": symbol, "date": day.isoformat(), "message": "入库队列已满"}
        return {"status": "queued", "symbol": symbol, "date": day.isoformat(), "coalesced": False}

    def status(self, symbol: str, trade_date: date | str | None = None) -> dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        day = _parse_date(trade_date) or date.today()
        key = (symbol, day)
        with self._lock:
            current = dict(self._status.get(key) or {})
            pending_count = len(self._pending)
        mysql_status: dict[str, Any] | None = None
        if trade_tick_mysql_store.configured():
            try:
                mysql_status = trade_tick_mysql_store.day_status(symbol, day)
            except Exception as e:
                mysql_status = {"ok": False, "error": str(e)}
        if not current:
            current = {"status": "idle", "symbol": symbol, "date": day.isoformat()}
        current = self._with_runtime_status(current, pending_count)
        current["mysql"] = mysql_status
        return current

    def _with_runtime_status(self, current: dict[str, Any], pending_count: int) -> dict[str, Any]:
        status = current.get("status")
        if status not in {"queued", "running"}:
            return current

        marker = current.get("started_at") or current.get("queued_at")
        elapsed = _elapsed_seconds(marker)
        timeout = max(30, int(settings.trade_ticks_persist_timeout_seconds or 120))
        current["elapsed_seconds"] = elapsed
        current["timeout_seconds"] = timeout
        current["queue_size"] = self._queue.qsize()
        current["pending_count"] = pending_count
        current["worker_alive"] = bool(self._thread and self._thread.is_alive())

        if elapsed is not None and elapsed > timeout:
            current["status"] = "timeout"
            current["error"] = (
                f"逐笔成交保存超过 {timeout}s 未完成, 可能是 tdx-api 拉取或 MySQL 写入卡住."
                "请稍后重试, 或重启后端/tdx-api sidecar."
            )
        return current

    def _run_loop(self) -> None:
        while self._running:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_task(task)
            except Exception:
                logger.exception("trade tick ingest unexpected failure")
            finally:
                with self._lock:
                    self._pending.discard(task.key)
                self._queue.task_done()

    def _run_task(self, task: TradeTickIngestTask) -> None:
        key = task.key
        interval = max(0, int(settings.trade_ticks_persist_interval_seconds or 0))
        now = time.monotonic()
        with self._lock:
            last = self._last_saved.get(key)
            if last and not task.force and now - last < interval:
                self._status[key] = {
                    "status": "skipped",
                    "symbol": task.symbol,
                    "date": task.trade_date.isoformat(),
                    "message": f"{interval}s 内已保存过, 跳过重复入库",
                    "finished_at": _now_iso(),
                    "rows": None,
                }
                return
            self._status[key] = {
                "status": "running",
                "symbol": task.symbol,
                "date": task.trade_date.isoformat(),
                "started_at": _now_iso(),
                "error": None,
            }

        try:
            provider = TDXAPIProvider()
            try:
                rows = provider.get_trade_ticks(task.symbol, task.trade_date, mode="all", limit=None)
            finally:
                provider.close()
            written = trade_tick_mysql_store.upsert_ticks(rows)
            with self._lock:
                self._last_saved[key] = time.monotonic()
                self._status[key] = {
                    "status": "succeeded",
                    "symbol": task.symbol,
                    "date": task.trade_date.isoformat(),
                    "rows": written,
                    "finished_at": _now_iso(),
                    "error": None,
                }
            logger.info("trade ticks persisted: %s %s rows=%d", task.symbol, task.trade_date, written)
        except Exception as e:
            logger.warning("trade ticks persist failed: %s %s: %s", task.symbol, task.trade_date, e)
            with self._lock:
                self._status[key] = {
                    "status": "failed",
                    "symbol": task.symbol,
                    "date": task.trade_date.isoformat(),
                    "finished_at": _now_iso(),
                    "error": str(e),
                }


def _parse_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _elapsed_seconds(value: Any) -> int | None:
    if not value:
        return None
    try:
        started = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return max(0, int((datetime.now() - started).total_seconds()))


trade_tick_ingestor = TradeTickIngestor()
