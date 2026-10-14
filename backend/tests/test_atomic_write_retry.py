"""Windows 读锁竞态下 parquet 原子替换的重试测试。

根因: polars scan_parquet / DuckDB read_parquet 扫描进行中持有分区句柄,
Windows os.replace 替换"仍被打开"的目标文件抛 PermissionError (WinError 5);
Linux 的 inode 交换语义无此限制。表现为个股分时"补齐数据"500。

修复: replace_with_retry 短退避重试穿过读窗口; 永久占用则原样抛出。
两处 _atomic_write_parquet (repository / kline_sync) 均接入。

另含 DuckDB 句柄泄漏回归: latest_minute_date 等曾用 self.db.execute(...)
.fetchone() 直连共享连接, 未消费结果集把首个分区句柄钉死在连接上,
导致同步 os.replace 永久被拒 (修为 execute_one cursor+close)。
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

import polars as pl
import pytest

from app.parquet import replace_with_retry
from app.services import kline_sync
from app.tickflow import repository

try:
    import psutil

    _PSUTIL = True
except ImportError:  # pragma: no cover
    _PSUTIL = False


def _minute_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [datetime(2026, 1, 15, 9, 30)],
        "open": [10.0], "high": [10.5], "low": [9.5], "close": [10.2],
        "volume": [100.0], "amount": [1020.0],
    })


def _flaky_replace(monkeypatch, fail_times: int) -> dict:
    """os.replace 前 fail_times 次 raise PermissionError, 之后正常执行。"""
    real_replace = os.replace
    state = {"calls": 0}

    def _flaky(src, dst):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    return state


# ---------- replace_with_retry 本体 ----------

def test_retry_succeeds_after_transient_blocks(tmp_path, monkeypatch):
    out = tmp_path / "part.parquet"
    out.write_bytes(b"old")
    src = tmp_path / "part.parquet.tmp"
    src.write_bytes(b"new")
    state = _flaky_replace(monkeypatch, fail_times=2)

    replace_with_retry(src, out, attempts=5, delay_s=0)

    assert out.read_bytes() == b"new"
    assert state["calls"] == 3
    assert not src.exists()


def test_retry_exhausted_raises_last_error(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")))
    src = tmp_path / "a.tmp"
    src.write_bytes(b"x")

    with pytest.raises(PermissionError, match="拒绝访问"):
        replace_with_retry(src, tmp_path / "a.parquet", attempts=3, delay_s=0)
    assert src.exists()  # 未被消费, 目标未生成


def test_retry_no_block_single_attempt(tmp_path, monkeypatch):
    out = tmp_path / "part.parquet"
    out.write_bytes(b"old")
    src = tmp_path / "part.parquet.tmp"
    src.write_bytes(b"new")
    state = _flaky_replace(monkeypatch, fail_times=0)

    replace_with_retry(src, out, attempts=5, delay_s=0)

    assert state["calls"] == 1
    assert out.read_bytes() == b"new"


# ---------- 两处 _atomic_write_parquet 接入 ----------

def test_kline_sync_atomic_write_survives_transient_lock(tmp_path, monkeypatch):
    state = _flaky_replace(monkeypatch, fail_times=1)
    out = tmp_path / "date=2026-01-15" / "part.parquet"
    out.parent.mkdir(parents=True)

    kline_sync._atomic_write_parquet(_minute_frame(), out)

    assert out.exists()
    assert state["calls"] == 2
    assert pl.read_parquet(out).height == 1


def test_repository_atomic_write_survives_transient_lock(tmp_path, monkeypatch):
    state = _flaky_replace(monkeypatch, fail_times=1)
    out = tmp_path / "kline_minute" / "date=2026-01-15" / "part.parquet"
    out.parent.mkdir(parents=True)

    repository.KlineRepository._atomic_write_parquet(_minute_frame(), out)

    assert out.exists()
    assert state["calls"] == 2


def test_write_minute_partition_survives_reader_race(tmp_path, monkeypatch):
    """集成: _write_minute_partition 读旧→concat→写新全程有读锁竞态仍完成。"""
    state = _flaky_replace(monkeypatch, fail_times=2)
    # 预置旧分区 (读改写路径)
    old_dir = tmp_path / "date=2026-01-15"
    old_dir.mkdir(parents=True)
    _minute_frame().write_parquet(old_dir / "part.parquet")

    written = kline_sync._write_minute_partition(_minute_frame(), tmp_path)

    assert written == 1
    assert state["calls"] >= 3  # 至少经历了重试


# ---------- DuckDB 句柄泄漏回归 (Windows 实测语义) ----------

@pytest.mark.skipif(sys.platform != "win32" or not _PSUTIL, reason="Windows 句柄语义 + psutil")
def test_minute_date_queries_do_not_pin_partition_handles(tmp_path):
    """latest_minute_date 等查询后不得残留分区句柄。

    旧实现 self.db.execute(...).fetchone() 的未消费结果集经 DuckDB buffer
    manager 钉住首个分区句柄, 后续同步 os.replace 永久 PermissionError。
    """
    from app.tickflow.repository import DataStore, KlineRepository

    minute_dir = tmp_path / "kline_minute"
    kline_sync._write_minute_partition(
        _minute_frame(), minute_dir)  # date=2026-01-15
    repo = KlineRepository(DataStore(data_dir=tmp_path))

    assert repo.latest_minute_date("600519.SH") == date(2026, 1, 15)
    assert repo.latest_minute_date_global() == date(2026, 1, 15)
    assert repo.earliest_minute_date() == date(2026, 1, 15)

    me = psutil.Process()
    held = [f.path for f in me.open_files() if "kline_minute" in f.path]
    assert held == []

    # 钉住场景的端到端后果: 查询后重写同一分区必须成功 (旧实现在此 PermissionError)
    assert kline_sync._write_minute_partition(_minute_frame(), minute_dir) == 1


# ---------- 跨进程文件锁与并发隔离测试 ----------

def test_interprocess_file_lock_mutual_exclusion(tmp_path):
    from app.parquet import interprocess_file_lock

    lock_file = tmp_path / ".test.lock"
    acquired = []

    with interprocess_file_lock(lock_file, timeout_s=2.0):
        acquired.append(1)
        # 在同一个锁内尝试再次以短超时获取锁应超时 (互斥性)
        with pytest.raises(TimeoutError):
            with interprocess_file_lock(lock_file, timeout_s=0.2):
                pass

    # 出锁后应能顺利再次获取
    with interprocess_file_lock(lock_file, timeout_s=2.0):
        acquired.append(2)

    assert acquired == [1, 2]


def test_atomic_write_parquet_cleans_tmp_on_failure(tmp_path, monkeypatch):
    from app.parquet import atomic_write_parquet

    out = tmp_path / "target.parquet"
    df = pl.DataFrame({"a": [1, 2, 3]})

    # 模拟 replace 失败抛出异常
    monkeypatch.setattr(
        "app.parquet.replace_with_retry",
        lambda s, d, **kw: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    with pytest.raises(RuntimeError, match="disk full"):
        atomic_write_parquet(df, out)

    # 验证没有任何残余的 .tmp 文件
    tmp_files = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob(".*.tmp"))
    assert tmp_files == []


def test_atomic_write_parquet_concurrent_isolation(tmp_path):
    """验证并发写入时各自的临时文件隔离，最终内容有效且无残留。"""
    from concurrent.futures import ThreadPoolExecutor
    from app.parquet import atomic_write_parquet

    out = tmp_path / "shared.parquet"

    def _worker(val: int):
        df = pl.DataFrame({"val": [val] * 100})
        atomic_write_parquet(df, out)

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_worker, i) for i in range(8)]
        for f in futures:
            f.result()

    assert out.exists()
    final_df = pl.read_parquet(out)
    assert final_df.height == 100
    tmp_files = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob(".*.tmp"))
    assert tmp_files == []


def test_atomic_update_parquet_concurrent_merge(tmp_path):
    """验证多线程并发进行增量合并读改写时，全部增量均保留，零覆盖丢失。"""
    from concurrent.futures import ThreadPoolExecutor
    from app.parquet import atomic_update_parquet

    out = tmp_path / "merged.parquet"
    # 写入初始基底数据 BASE
    base_df = pl.DataFrame({
        "symbol": ["BASE"] * 5,
        "datetime": [datetime(2026, 1, 15, 9, 30 + i) for i in range(5)],
        "val": [100.0] * 5,
    })
    base_df.write_parquet(out)

    num_workers = 6
    rows_per_worker = 10

    def _worker(worker_id: int):
        worker_df = pl.DataFrame({
            "symbol": [f"STOCK_{worker_id}"] * rows_per_worker,
            "datetime": [datetime(2026, 1, 15, 9, 30 + i) for i in range(rows_per_worker)],
            "val": [float(worker_id)] * rows_per_worker,
        })

        def merge(existing: pl.DataFrame) -> pl.DataFrame:
            combined = pl.concat([existing, worker_df]) if not existing.is_empty() else worker_df
            return combined.unique(subset=["symbol", "datetime"], keep="last").sort(["symbol", "datetime"])

        atomic_update_parquet(out, merge)

    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        futures = [ex.submit(_worker, i) for i in range(num_workers)]
        for f in futures:
            f.result()

    assert out.exists()
    final_df = pl.read_parquet(out)
    # 总行数 = BASE (5) + 6 个 worker 各 10 行 = 65 行
    assert final_df.height == 5 + num_workers * rows_per_worker
    symbols = set(final_df["symbol"].unique().to_list())
    expected_symbols = {"BASE"} | {f"STOCK_{i}" for i in range(num_workers)}
    assert symbols == expected_symbols


def test_write_minute_partition_concurrent_merge_keeps_all_symbols(tmp_path):
    """针对 kline_sync._write_minute_partition 的并发隔离回归测试：
    验证两个并发任务写入同一天不同标的（A 与 B）至包含 BASE 的同一日分区时，
    最终分区内完整保留 BASE、A、B，杜绝读改写竞态丢数据。
    """
    from concurrent.futures import ThreadPoolExecutor

    minute_dir = tmp_path / "kline_minute"
    trade_date = "2026-01-15"

    # 先写入基底 BASE
    base_df = pl.DataFrame({
        "_trade_date": [trade_date],
        "symbol": ["BASE"],
        "datetime": [datetime(2026, 1, 15, 9, 30)],
        "open": [10.0], "high": [10.5], "low": [9.5], "close": [10.2],
        "volume": [100.0], "amount": [1020.0],
    })
    kline_sync._write_minute_partition(base_df, minute_dir)

    # 准备标的 A 与标的 B
    df_a = pl.DataFrame({
        "_trade_date": [trade_date] * 2,
        "symbol": ["000001.SZ"] * 2,
        "datetime": [datetime(2026, 1, 15, 9, 30), datetime(2026, 1, 15, 9, 31)],
        "open": [11.0, 11.1], "high": [11.2, 11.3], "low": [10.9, 11.0], "close": [11.1, 11.2],
        "volume": [200.0, 210.0], "amount": [2220.0, 2350.0],
    })
    df_b = pl.DataFrame({
        "_trade_date": [trade_date] * 2,
        "symbol": ["000002.SZ"] * 2,
        "datetime": [datetime(2026, 1, 15, 9, 30), datetime(2026, 1, 15, 9, 31)],
        "open": [12.0, 12.1], "high": [12.2, 12.3], "low": [11.9, 12.0], "close": [12.1, 12.2],
        "volume": [300.0, 310.0], "amount": [3630.0, 3750.0],
    })

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_a = ex.submit(kline_sync._write_minute_partition, df_a, minute_dir)
        f_b = ex.submit(kline_sync._write_minute_partition, df_b, minute_dir)
        f_a.result()
        f_b.result()

    part_file = minute_dir / f"date={trade_date}" / "part.parquet"
    assert part_file.exists()
    final_df = pl.read_parquet(part_file)

    # 预期保留 BASE (1) + A (2) + B (2) = 5 行
    assert final_df.height == 5
    symbols = set(final_df["symbol"].unique().to_list())
    assert symbols == {"BASE", "000001.SZ", "000002.SZ"}
