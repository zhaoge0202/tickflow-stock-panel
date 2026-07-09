"""盘中提醒回放 API。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import intraday_replay

router = APIRouter(prefix="/api/replay", tags=["replay"])


class IntradayReplayReq(BaseModel):
    date: date
    symbols: list[str]
    start_time: str | None = None
    end_time: str | None = None


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


@router.post("/intraday")
def run_intraday(req: IntradayReplayReq, request: Request):
    symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    return intraday_replay.enqueue_replay(
        _data_dir(request),
        target_date=req.date,
        symbols=symbols,
        start_time=req.start_time,
        end_time=req.end_time,
    )


@router.get("/intraday/{task_id}")
def get_intraday(task_id: str):
    task = intraday_replay.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="回放任务不存在")
    return task
