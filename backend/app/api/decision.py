"""盘中决策台 API。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services import decision_queue

router = APIRouter(prefix="/api/decision", tags=["decision"])


class DecisionActionReq(BaseModel):
    action: str
    side: str | None = None
    price: float | None = None
    note: str = ""


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


@router.get("/queue")
def queue(
    request: Request,
    date_: str | None = Query(default=None, alias="date"),
    status: str | None = None,
):
    target_date = _parse_date(date_)
    return decision_queue.build_queue(
        _data_dir(request),
        request.app.state.repo,
        target_date=target_date,
        status=status,
    )


@router.get("/summary")
def summary(
    request: Request,
    date_: str | None = Query(default=None, alias="date"),
):
    return decision_queue.summary(
        _data_dir(request),
        request.app.state.repo,
        target_date=_parse_date(date_),
    )


@router.get("/items/{symbol}")
def item(
    symbol: str,
    request: Request,
    date_: str | None = Query(default=None, alias="date"),
):
    out = decision_queue.get_item(
        _data_dir(request),
        request.app.state.repo,
        symbol,
        target_date=_parse_date(date_),
    )
    if out is None:
        raise HTTPException(status_code=404, detail="决策项不存在")
    return out


@router.post("/items/{symbol}/action")
def action(symbol: str, req: DecisionActionReq, request: Request):
    try:
        return decision_queue.record_action(
            _data_dir(request),
            request.app.state.repo,
            symbol,
            req.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/timeline/{symbol}")
def timeline(
    symbol: str,
    request: Request,
    date_: str | None = Query(default=None, alias="date"),
):
    return {
        "symbol": symbol.upper(),
        "events": decision_queue.timeline(
            _data_dir(request),
            symbol,
            request.app.state.repo,
            target_date=_parse_date(date_),
        ),
    }
