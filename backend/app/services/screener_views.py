"""策略页辅助视图复用既有计算队列, HTTP 只读取任务快照。"""
from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from fastapi import HTTPException

from app.services import screener_work, strategy_run_queue
from app.services.shared_work import WorkBusyError

_RESULT_ID = "view"


def _file_version(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _source_version(repo, kind: str, as_of: date, asset_type: str, timeframe: str, parameters: dict) -> tuple:
    root = Path(repo.store.data_dir)
    # 盘中视图使用一次冻结的当前行情; 只对稳定历史和硬修正做失效,
    # 不能因每轮今日行情/分钟落盘而永远无法完成。
    live = bool(parameters.get("live_snapshot"))
    history_end = as_of - timedelta(days=1) if live else as_of
    version = (
        screener_work.source_version(repo, asset_type, timeframe, history_end),
        _file_version(root / "local_config" / "auction_strategy.json"),
    )
    if kind == "focus":
        # 14:50 初选依赖当天分钟数据与真实预览快照, 不能仅按日线代际复用。
        sid = parameters.get("strategy_id")
        if not live:
            version += (screener_work.source_version(repo, asset_type, "1m", as_of),)
        version += (_file_version(root / "user_data" / f"preview_{sid}_{as_of}.json"),)
    return version


def background_view(
    repo, engine, *, kind: str, as_of: date, asset_type: str, timeframe: str,
    parameters: dict, compute: Callable[[], dict], pending: dict,
    on_submit: Callable[[], None] | None = None,
) -> dict:
    """同输入合并后台任务; 参数/定义/数据换代后绝不返回旧任务结果。"""
    identity = screener_work.hard_identity(repo, engine, asset_type)
    version = _source_version(repo, kind, as_of, asset_type, timeframe, parameters)
    key = (
        "view", kind, asset_type, timeframe, str(as_of), identity,
        screener_work.digest(parameters), screener_work.digest(version),
    )

    def validate() -> None:
        screener_work.assert_current(repo, engine, asset_type, identity)
        if _source_version(repo, kind, as_of, asset_type, timeframe, parameters) != version:
            raise WorkBusyError("策略依赖数据已更新，请重新获取结果")

    def job(handle: strategy_run_queue.StrategyRunHandle) -> None:
        try:
            validate()
            payload = compute()
            validate()
        except ValueError as exc:
            handle.fail(str(exc), status_code=404 if "unknown strateg" in str(exc) else 400)
            return
        except HTTPException as exc:
            handle.fail(str(exc.detail), status_code=exc.status_code)
            return
        handle.complete(_RESULT_ID, payload)

    try:
        handle = strategy_run_queue.MANAGER.get_or_submit(
            key, [_RESULT_ID], job,
            reuse_completed_ms=5000 if parameters.get("live_snapshot") else strategy_run_queue.MANAGER.KEEP_DONE_MS,
            source_version=version, on_submit=on_submit, failure_reuse_ms=10_000,
        )
    except WorkBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "3"}) from exc
    snapshot = handle.snapshot()
    if snapshot["error"]:
        raise HTTPException(
            status_code=snapshot["error_status"], detail=snapshot["error"],
            headers={"Retry-After": "3"} if snapshot["error_status"] == 503 else None,
        )
    payload = snapshot["results"].get(_RESULT_ID)
    if payload is not None:
        try:
            validate()
        except WorkBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "3"}) from exc
        return {
            **copy.deepcopy(payload),
            "calculation": {"status": "ready", "task_id": handle.id},
        }
    return {
        **copy.deepcopy(pending),
        "calculation": {"status": "pending", "task_id": handle.id},
    }
