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

    repository.replace_with_retry(src, out, attempts=5, delay_s=0)

    assert out.read_bytes() == b"new"
    assert state["calls"] == 3
    assert not src.exists()


def test_retry_exhausted_raises_last_error(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")))
    src = tmp_path / "a.tmp"
    src.write_bytes(b"x")

    with pytest.raises(PermissionError, match="拒绝访问"):
        repository.replace_with_retry(src, tmp_path / "a.parquet", attempts=3, delay_s=0)
    assert src.exists()  # 未被消费, 目标未生成


def test_retry_no_block_single_attempt(tmp_path, monkeypatch):
    out = tmp_path / "part.parquet"
    out.write_bytes(b"old")
    src = tmp_path / "part.parquet.tmp"
    src.write_bytes(b"new")
    state = _flaky_replace(monkeypatch, fail_times=0)

    repository.replace_with_retry(src, out, attempts=5, delay_s=0)

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
