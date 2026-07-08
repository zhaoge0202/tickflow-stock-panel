"""告警后验收益 API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from app.services import alert_outcome

router = APIRouter(prefix="/api/alert-outcomes", tags=["alert-outcomes"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


@router.get("")
def list_outcomes(request: Request, days: int = 7, strategy_id: str | None = None):
    rows = alert_outcome.list_recent(_data_dir(request), days=days, strategy_id=strategy_id)
    return {"outcomes": rows, "total": len(rows)}


@router.get("/summary")
def summary(request: Request, group_by: str = "signal", days: int = 7):
    return alert_outcome.summary(_data_dir(request), group_by=group_by, days=days)


@router.post("/track")
def track(request: Request):
    rows = alert_outcome.track_pending(_data_dir(request))
    return {"ok": True, "updated": len(rows)}
