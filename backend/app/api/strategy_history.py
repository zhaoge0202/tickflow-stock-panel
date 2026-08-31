"""策略候选生命周期历史 API。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services import strategy_history

router = APIRouter(prefix="/api/strategy-history", tags=["strategy-history"])


@router.get("")
def list_strategy_history(
    request: Request,
    strategy_id: str | None = None,
    symbol: str | None = None,
    signal_date: str | None = None,
    trade_date: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    days: int = Query(180, ge=1, le=180),
    limit: int = Query(1000, ge=1, le=50000),
):
    events = strategy_history.list_events(
        request.app.state.repo.store.data_dir,
        strategy_id=strategy_id,
        symbol=symbol,
        signal_date=signal_date,
        trade_date=trade_date,
        event_type=event_type,
        status=status,
        days=days,
        limit=limit,
    )
    return {"events": events, "total": len(events)}
