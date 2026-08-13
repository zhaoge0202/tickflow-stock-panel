"""一次性修复: TDX ServerTime 分钟溢出误解析导致 quote_ticks event_ts 偏移。

实时轮询行 (source=tdxapi) 的 event_ts 与 ingest_ts 理应只差几秒;
历史 bug 使 event_ts 偏早 25-40 分钟, 甚至跨日落成未来时间。
本脚本把偏差超过 120 秒的实时行的 event_ts 重置为 ingest_ts,
并按新 event_ts 重算 hour 分区, 原分区整目录备份后原子替换。

用法: python fix_quote_tick_event_ts.py /app/data 2026-08-13 [更多日期...]
"""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

CN_TZ = timezone(timedelta(hours=8))
REALTIME_SOURCE = "tdxapi"
MAX_DIVERGENCE_MS = 120_000


def fix_date_partition(data_dir: Path, ds: str) -> None:
    base = data_dir / "quote_ticks" / f"date={ds}"
    if not base.exists():
        print(f"[{ds}] 分区不存在, 跳过")
        return

    tmp_base = data_dir / "quote_ticks" / f".fix-tmp-{ds}"
    if tmp_base.exists():
        shutil.rmtree(tmp_base)
    tmp_base.mkdir(parents=True)

    paths = sorted(base.rglob("*.parquet"))
    total_rows = 0
    fixed_rows = 0
    for index, path in enumerate(paths):
        df = pl.read_parquet(path)
        total_rows += df.height
        if {"event_ts", "ingest_ts", "source"} <= set(df.columns):
            needs_fix = (
                (pl.col("source") == REALTIME_SOURCE)
                & pl.col("ingest_ts").is_not_null()
                & ((pl.col("event_ts") - pl.col("ingest_ts")).abs() > MAX_DIVERGENCE_MS)
            )
            fixed_rows += df.filter(needs_fix).height
            df = df.with_columns(
                pl.when(needs_fix)
                .then(pl.col("ingest_ts"))
                .otherwise(pl.col("event_ts"))
                .alias("event_ts")
            )
            if "hour" in df.columns:
                hours = [
                    f"{datetime.fromtimestamp(ts / 1000, tz=CN_TZ).hour:02d}" if ts else hour
                    for ts, hour in zip(df["event_ts"].to_list(), df["hour"].to_list())
                ]
                df = df.with_columns(pl.Series("hour", hours, dtype=pl.Utf8))
        hour_col = df["hour"] if "hour" in df.columns else None
        if hour_col is None:
            groups = [("00", df)]
        else:
            groups = [(str(h[0]), part) for h, part in df.group_by("hour")] if df.height else []
        for hour, part in groups:
            target_dir = tmp_base / f"hour={hour}"
            target_dir.mkdir(parents=True, exist_ok=True)
            part.write_parquet(target_dir / f"part-fix-{index:05d}-{path.stem}.parquet")

    backup = data_dir / "quote_ticks" / f".bak-{ds}-{int(time.time())}"
    base.rename(backup)
    tmp_base.rename(base)
    print(f"[{ds}] files={len(paths)} rows={total_rows} fixed={fixed_rows} backup={backup.name}")


def main() -> None:
    data_dir = Path(sys.argv[1])
    for ds in sys.argv[2:]:
        fix_date_partition(data_dir, ds)


if __name__ == "__main__":
    main()
