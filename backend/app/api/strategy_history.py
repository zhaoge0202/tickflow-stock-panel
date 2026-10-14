"""策略候选生命周期历史 API。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.services import auction_replay, strategy_history

router = APIRouter(prefix="/api/strategy-history", tags=["strategy-history"])
logger = logging.getLogger(__name__)


class StrategyHistoryBackfillRequest(BaseModel):
    strategy_ids: list[str] | None = None
    max_cycles: int = Field(5, ge=1, le=10)
    asset_type: str = "stock"


@router.post("/backfill")
def backfill_strategy_history(
    body: StrategyHistoryBackfillRequest,
    request: Request,
):
    if body.asset_type != "stock":
        return {"cycles": 0, "written": 0}
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        return {"cycles": 0, "written": 0}
    strategy_ids = body.strategy_ids
    if strategy_ids is None:
        strategy_ids = [
            str(meta["id"])
            for meta in engine.list_strategies()
            if not meta.get("research_only")
            and "stock" in meta.get("asset_types", ["stock"])
            and "1d" in meta.get("timeframes", ["1d"])
        ]
    try:
        return auction_replay.backfill_recent_strategy_history(
            request.app.state.repo,
            engine,
            strategy_ids=strategy_ids,
            max_cycles=body.max_cycles,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("策略历史回填失败: %s", exc)
        return {"cycles": 0, "written": 0}


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
