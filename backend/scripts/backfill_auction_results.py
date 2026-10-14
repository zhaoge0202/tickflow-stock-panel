#!/usr/bin/env python3
"""按交易日补齐 TDX 09:25 最终竞价结果.

这个任务只补真实历史分笔中存在的 09:25 成交结果, 不把日线 open 当作
竞价价, 也不伪造 09:15-09:25 竞价过程快照. 每个交易日独立提交:

1. 找出当天 enriched 中缺少 auction_result 的标的;
2. 通过 tdx-api 历史分笔逐票查询;
3. 将真实结果追加到 quote_ticks;
4. 刷新当天 enriched;
5. 成功后写 checkpoint, 支持中断续跑.

示例 (Windows backend 目录):
    .venv\\Scripts\\python.exe scripts\\backfill_auction_results.py \
        --data-dir E:\\stock\\data --start 2025-10-01 --end 2026-09-07
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.indicators.pipeline import apply_auction_result_fields_to_enriched
from app.plugins.tdxapi.provider import TDXAPIProvider
from app.services import quote_tick_store

logger = logging.getLogger("backfill_auction_results")

DEFAULT_START = date(2025, 10, 1)
DEFAULT_END = date(2026, 9, 7)
DEFAULT_WORKERS = 4
MAX_WORKERS = 4
CHECKPOINT_VERSION = 1


class _WorkerProviders:
    """为每个线程复用一个 provider, 避免逐票创建 HTTP 客户端."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._all: list[TDXAPIProvider] = []
        self._lock = threading.Lock()

    def init_worker(self) -> None:
        provider = TDXAPIProvider()
        with self._lock:
            self._all.append(provider)
        self._local.provider = provider

    def get(self) -> TDXAPIProvider:
        provider = getattr(self._local, "provider", None)
        if provider is None:
            raise RuntimeError("竞价回补 worker provider 未初始化")
        return provider

    def close(self) -> None:
        with self._lock:
            providers = list(self._all)
            self._all.clear()
        for provider in providers:
            provider.close()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式错误: {value}, 应为 YYYY-MM-DD") from exc


def _date_partitions(data_dir: Path, start: date, end: date) -> list[date]:
    base = data_dir / "kline_daily_enriched"
    dates: list[date] = []
    if not base.exists():
        return dates
    for path in base.iterdir():
        if not path.is_dir() or not path.name.startswith("date="):
            continue
        try:
            target = date.fromisoformat(path.name[5:])
        except ValueError:
            continue
        if start <= target <= end and (path / "part.parquet").exists():
            dates.append(target)
    return sorted(dates)


def _load_symbols(data_dir: Path, target: date) -> list[str]:
    path = data_dir / "kline_daily_enriched" / f"date={target.isoformat()}" / "part.parquet"
    if not path.exists():
        return []
    frame = pl.read_parquet(path, columns=["symbol"])
    if frame.is_empty() or "symbol" not in frame.columns:
        return []
    return sorted({str(symbol).strip().upper() for symbol in frame["symbol"].to_list() if symbol})


def _checkpoint_path(data_dir: Path) -> Path:
    return data_dir / "auction_backfill" / "checkpoint.json"


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": CHECKPOINT_VERSION, "completed": {}, "updated_at": None}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"checkpoint 读取失败: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != CHECKPOINT_VERSION:
        raise RuntimeError(f"checkpoint 版本不兼容: {path}")
    value.setdefault("completed", {})
    return value


def _save_checkpoint(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _fetch_one(
    providers: _WorkerProviders,
    symbol: str,
    target: date,
) -> tuple[str, dict | None, str | None]:
    try:
        rows = providers.get().get_auction_results([symbol], target)
        return symbol, rows[0] if rows else None, None
    except Exception as exc:
        return symbol, None, str(exc)


def _run_day(data_dir: Path, target: date, workers: int) -> dict[str, Any]:
    symbols = _load_symbols(data_dir, target)
    enriched_symbols = set(symbols)
    existing = quote_tick_store.auction_result_fields(data_dir, target_date=target)
    existing_symbols = (
        set(existing["symbol"].to_list()) & enriched_symbols
        if not existing.is_empty() else set()
    )
    missing = [symbol for symbol in symbols if symbol not in existing_symbols]
    if not missing:
        applied = apply_auction_result_fields_to_enriched(data_dir, target)
        return {
            "symbols": len(symbols),
            "missing_before": 0,
            "historical_found": 0,
            "written": 0,
            "errors": 1 if applied.get("error") else 0,
            "enriched": applied,
            "error_sample": (
                [{"symbol": "<enriched>", "error": str(applied["error"])}]
                if applied.get("error") else []
            ),
        }

    providers = _WorkerProviders()
    found: list[dict] = []
    errors: list[dict[str, str]] = []
    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="auction-backfill",
            initializer=providers.init_worker,
        ) as pool:
            futures = [pool.submit(_fetch_one, providers, symbol, target) for symbol in missing]
            for index, future in enumerate(as_completed(futures), start=1):
                symbol, row, error = future.result()
                if row is not None:
                    found.append(row)
                if error:
                    errors.append({"symbol": symbol, "error": error})
                if index % 200 == 0 or index == len(futures):
                    logger.info(
                        "date=%s progress=%d/%d found=%d errors=%d",
                        target,
                        index,
                        len(futures),
                        len(found),
                        len(errors),
                    )
    finally:
        providers.close()

    records = [
        {
            "symbol": row["symbol"],
            "last_price": row["price"],
            "close": row["price"],
            "volume": row["volume"],
            "amount": row["amount"],
            "timestamp": row["auction_datetime"],
            "price_type": "trade",
            "market_phase": "opening_auction",
        }
        for row in found
        if row.get("symbol") not in existing_symbols
    ]
    if records:
        append_summary = quote_tick_store.append_many(
            data_dir,
            records,
            source="tdxapi_auction_result_history",
            force_flush=True,
        )
    else:
        append_summary = {}

    applied = apply_auction_result_fields_to_enriched(data_dir, target)
    if applied.get("error"):
        errors.append({"symbol": "<enriched>", "error": str(applied["error"])})
    found_symbols = {
        str(row.get("symbol") or "") for row in found
    } & enriched_symbols
    expected_populated = len(existing_symbols | found_symbols)
    if (
        expected_populated > 0
        and int(applied.get("populated", 0)) < expected_populated
    ):
        errors.append({
            "symbol": "<enriched>",
            "error": (
                f"enriched 竞价覆盖不足: "
                f"{applied.get('populated', 0)}/{expected_populated}"
            ),
        })
    if records and int(append_summary.get("written", 0)) < len(records):
        errors.append({
            "symbol": "<quote_ticks>",
            "error": (
                f"quote_ticks 写入不足: "
                f"{append_summary.get('written', 0)}/{len(records)}"
            ),
        })
    return {
        "symbols": len(symbols),
        "missing_before": len(missing),
        "historical_found": len(found),
        "written": len(records),
        "unresolved": len(set(missing) - found_symbols),
        "errors": len(errors),
        "append_summary": append_summary,
        "enriched": applied,
        "error_sample": errors[:5],
    }


def run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).resolve()
    dates = _date_partitions(data_dir, args.start, args.end)
    if args.max_days > 0:
        dates = dates[:args.max_days]
    if not dates:
        raise RuntimeError(f"没有找到 enriched 交易日分区: {data_dir}")

    checkpoint_path = _checkpoint_path(data_dir)
    checkpoint = _load_checkpoint(checkpoint_path)
    completed = checkpoint.setdefault("completed", {})
    workers = max(1, min(MAX_WORKERS, int(args.workers)))
    logger.info(
        "auction backfill start: data=%s dates=%d range=%s~%s workers=%d",
        data_dir,
        len(dates),
        dates[0],
        dates[-1],
        workers,
    )

    for index, target in enumerate(dates, start=1):
        key = target.isoformat()
        if not args.force and key in completed and completed[key].get("status") == "succeeded":
            logger.info("skip completed date=%s (%d/%d)", target, index, len(dates))
            continue
        started = time.perf_counter()
        result = _run_day(data_dir, target, workers)
        result.update({
            "status": "partial" if result.get("errors", 0) or result.get("unresolved", 0) else "succeeded",
            "date": key,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        })
        completed[key] = result
        checkpoint["updated_at"] = date.today().isoformat()
        _save_checkpoint(checkpoint_path, checkpoint)
        logger.info("date=%s completed: %s", target, result)

    logger.info("auction backfill finished: checkpoint=%s", checkpoint_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="逐交易日补齐 TDX 09:25 最终竞价结果")
    parser.add_argument("--data-dir", required=True, help="本地数据目录")
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--end", type=_parse_date, default=DEFAULT_END)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-days", type=int, default=0, help="本次最多处理多少个交易日, 0 表示全部")
    parser.add_argument("--force", action="store_true", help="忽略已完成 checkpoint, 重新核对每一天")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(args)
    except Exception as exc:
        logger.exception("auction backfill failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
