"""polars 并发死锁复现压测。

模拟 2026-09-07 线上事故的触发形态: 多个线程并发对同一 parquet 目录做
lazy scan + filter + collect (页面首屏多路读), 叠加后台线程的批量重计算 —
全部绕过 polars_guard 闸, 直接裸调 collect。

判定: worker 线程持续完成 collect 即健康; 若超过宽限期没有任何完成
(0 进度推进), 判定死锁复现, 退出码 1。

用法:
    uv run python scripts/stress_polars_concurrency.py [--duration 180] [--threads 8]
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl


def _build_dataset(root: Path, symbols: int = 300, days: int = 120) -> None:
    """构造 ~数十万行、按日期分区的 parquet 目录 (模拟 enriched 布局)。"""
    dates = pl.date_range(
        __import__("datetime").date(2026, 1, 1),
        __import__("datetime").date(2026, 12, 31),
        "1d",
        eager=True,
    ).to_list()[:days]
    for d in dates:
        df = pl.DataFrame({
            "symbol": [f"{i:06d}.SZ" for i in range(symbols)],
            "date": [d] * symbols,
            "close": [10.0 + (i % 37) for i in range(symbols)],
            "volume": [1_000.0 * (i + 1) for i in range(symbols)],
        })
        out = root / f"date={d.isoformat()}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--grace", type=float, default=60.0)
    args = parser.parse_args()

    stop = threading.Event()
    stats = {"collects": 0, "last_done": time.monotonic()}
    lock = threading.Lock()

    with TemporaryDirectory(prefix="polars_stress_") as tmp:
        root = Path(tmp) / "kline"
        print("building dataset ...", flush=True)
        _build_dataset(root)
        glob = str(root / "**" / "*.parquet")

        def worker(kind: str) -> None:
            i = 0
            while not stop.is_set():
                i += 1
                if kind == "interactive":
                    df = (
                        pl.scan_parquet(glob)
                        .filter((pl.col("symbol") == f"{(i * 7) % 300:06d}.SZ"))
                        .sort("date")
                        .collect()
                    )
                else:
                    df = (
                        pl.scan_parquet(glob)
                        .filter(pl.col("volume") > 100_000.0)
                        .group_by("symbol")
                        .agg(pl.col("close").mean().alias("avg_close"))
                        .sort("symbol")
                        .collect()
                    )
                del df
                with lock:
                    stats["collects"] += 1
                    stats["last_done"] = time.monotonic()

        print(f"stressing: {args.threads} threads for {args.duration:.0f}s "
              f"(polars {pl.__version__})", flush=True)
        threads = [
            threading.Thread(
                target=worker,
                args=("interactive" if i % 2 == 0 else "background",),
                daemon=True,
            )
            for i in range(args.threads)
        ]
        for t in threads:
            t.start()

        deadline = time.monotonic() + args.duration
        wedged = False
        while time.monotonic() < deadline:
            time.sleep(5.0)
            with lock:
                idle = time.monotonic() - stats["last_done"]
                done = stats["collects"]
            print(f"  progress: {done} collects, idle {idle:.1f}s", flush=True)
            if idle > args.grace:
                wedged = True
                break
        stop.set()
        for t in threads:
            t.join(timeout=10)

        alive = [t for t in threads if t.is_alive()]
        with lock:
            total = stats["collects"]
        if wedged or alive:
            print(f"RESULT: DEADLOCK REPRODUCED — wedged={wedged}, "
                  f"stuck_threads={len(alive)}/{args.threads}, total_collects={total}", flush=True)
            return 1
        print(f"RESULT: HEALTHY — {total} collects completed, all threads exited", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
