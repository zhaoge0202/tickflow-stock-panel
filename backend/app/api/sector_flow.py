"""板块强度与资金流 API。"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.market_time import cn_today
from app.services.sector_flow import build_sector_flow_series

router = APIRouter(prefix="/api/sector-flow", tags=["sector-flow"])


@router.get("/series")
def sector_flow_series(
    request: Request,
    kind: Annotated[Literal["concept", "industry"], Query()] = "industry",
    metric: Annotated[Literal["strength", "main_flow"], Query()] = "main_flow",
    trade_date: Annotated[date | None, Query(alias="date")] = None,
    step_seconds: Annotated[int, Query(ge=30, le=600)] = 60,
    limit: Annotated[int, Query(ge=1, le=120)] = 24,
    level: Annotated[int | None, Query(ge=1, le=3)] = None,
):
    repo = getattr(request.app.state, "repo", None)
    sector_service = getattr(request.app.state, "sector_monitor_service", None)
    if repo is None or sector_service is None:
        raise HTTPException(status_code=503, detail="板块服务尚未初始化")
    target_date = trade_date or cn_today()
    return build_sector_flow_series(
        repo=repo,
        sector_service=sector_service,
        kind=kind,
        metric=metric,
        trade_date=target_date,
        step_seconds=step_seconds,
        limit=limit,
        level=level,
    )
