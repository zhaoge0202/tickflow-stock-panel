"""回归测试: 卡死判定从「总时长一刀切」改为「进度停滞」+ 协作式取消 + 执行槽所有权。

背景(用户反馈): 慢带宽环境冷启动全市场拉取超过 20 分钟被误标失败,
拉取线程(僵尸)仍在写盘, UI 状态与实际不对齐; 重复点击还可能撞执行锁。
均为纯逻辑, 不触网。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import pipeline_jobs, preferences
from app.services.pipeline_jobs import JobCancelledError, JobStore


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """取消标志与执行槽是模块级单例, 每个用例前后复位, 避免相互污染。"""
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs._run_slot_owner = None
    yield
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs._run_slot_owner = None


def _make_running_job(store: JobStore, timeout_s: int) -> str:
    jid, _ = store.create(timeout_s=timeout_s)
    store.start(jid)
    return jid


# ── 进度停滞判定 ────────────────────────────────────────────────────────

def test_stalled_job_is_reaped(monkeypatch, tmp_path):
    """无进度上报超过阈值 → 标记失败 + 置取消标志 + 释放执行槽。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    # 启动后 5 分钟无任何进度 → 停滞 300s > 60s
    stale = _iso(_now() - timedelta(minutes=5))
    store._active_jobs[jid]["started_at"] = stale
    store._active_jobs[jid]["last_progress_at"] = stale

    assert pipeline_jobs.try_acquire_run_slot(jid) is True
    store.reap_stale()

    j = store.get(jid)
    assert j["status"] == "failed"
    assert "进度停滞" in j["error"]
    # 协作式取消: 僵尸线程通过 flag 感知(记录已被 fail 弹出, flag 仍在)
    assert pipeline_jobs.is_cancelled(jid)
    # 执行槽已按所有权释放
    assert pipeline_jobs.try_acquire_run_slot("next") is True


def test_progressing_job_is_not_reaped(monkeypatch, tmp_path):
    """慢但在推进: 总时长远超阈值, 但进度心跳新鲜 → 不得误杀(核心回归)。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    # 总时长 2 小时(远超 60s 阈值), 但 10 秒前刚上报过进度
    store._active_jobs[jid]["started_at"] = _iso(_now() - timedelta(hours=2))
    store._active_jobs[jid]["last_progress_at"] = _iso(_now() - timedelta(seconds=10))

    store.reap_stale()
    assert store.get(jid)["status"] == "running"


def test_hard_cap_terminates_endless_progress(tmp_path):
    """进度回调持续但总时长超硬上限 → 兜底终止。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    beyond = timedelta(seconds=pipeline_jobs.HARD_JOB_TIMEOUT_S + 3600)
    store._active_jobs[jid]["started_at"] = _iso(_now() - beyond)
    store._active_jobs[jid]["last_progress_at"] = _iso(_now())

    store.reap_stale()
    j = store.get(jid)
    assert j["status"] == "failed"
    assert "硬上限" in j["error"]


def test_progress_updates_heartbeat(tmp_path):
    """progress() 刷新 last_progress_at(停滞计时的基准)。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    store.progress(jid, "sync", 10, "chunk 1/10")
    assert store.get(jid)["last_progress_at"] is not None


# ── 协作式取消 ──────────────────────────────────────────────────────────

def test_progress_raises_after_cancel(tmp_path):
    """取消后, 僵尸线程下一次 progress() 回调抛 JobCancelledError 自行退出。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)

    pipeline_jobs.request_cancel(jid)
    with pytest.raises(JobCancelledError):
        store.progress(jid, "sync", 20, "chunk 2/10")


def test_progress_raises_after_record_popped(tmp_path):
    """terminate() 已把记录弹出后, flag 仍需生效(僵尸靠 flag 而非记录感知)。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    store.terminate(jid, "超时自动取消")

    # 记录已从内存弹出
    assert store.get(jid)["status"] == "failed"
    with pytest.raises(JobCancelledError):
        store.progress(jid, "sync", 20, "zombie chunk")


def test_cancelled_error_survives_chunk_isolation():
    """JobCancelledError 继承 BaseException: 同步循环的分块异常隔离不得吞掉它。"""
    def chunk_loop(cancel_at: int) -> str:
        for i in range(5):
            try:
                if i == cancel_at:
                    raise JobCancelledError("j1")
            except Exception:  # noqa: BLE001  — 分块隔离的典型写法
                continue
        return "completed"

    with pytest.raises(JobCancelledError):
        chunk_loop(2)


# ── 执行槽所有权 ────────────────────────────────────────────────────────

def test_run_slot_ownership_guard():
    """非持有者的释放一律忽略 —— 僵尸线程 finally 不得误释放新任务的槽。"""
    assert pipeline_jobs.try_acquire_run_slot("jobA") is True
    assert pipeline_jobs.try_acquire_run_slot("jobB") is False

    # 旧 job(僵尸)的 finally 释放: 槽属于 jobA, 忽略
    pipeline_jobs.release_run_slot("jobB")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is False

    # 持有者自己释放后才可用
    pipeline_jobs.release_run_slot("jobA")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is True
    pipeline_jobs.release_run_slot("jobC")


def test_run_slot_reap_release_prevents_zombie_release():
    """reap 强制释放后, 僵尸晚到的同 owner 释放是幂等 no-op, 不影响新持有者。"""
    assert pipeline_jobs.try_acquire_run_slot("jobA") is True
    pipeline_jobs.release_run_slot("jobA")  # terminate 的强制释放

    assert pipeline_jobs.try_acquire_run_slot("jobB") is True  # 新任务立即入槽
    pipeline_jobs.release_run_slot("jobA")  # 僵尸 finally: owner 不匹配 → 忽略
    assert pipeline_jobs.try_acquire_run_slot("jobC") is False  # jobB 仍持有

    pipeline_jobs.release_run_slot("jobB")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is True
    pipeline_jobs.release_run_slot("jobC")
