"""候选池高频盯盘状态接口。"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/candidate-watch", tags=["candidate-watch"])


@router.get("/status")
def candidate_watch_status(request: Request) -> dict:
    """候选池盯盘运行状态: 池大小、周期、今日触发数、最近一次触发。"""
    cw = getattr(request.app.state, "candidate_watch", None)
    if cw is None:
        return {"enabled": False, "running": False, "reason": "not_initialized"}
    return cw.snapshot()
