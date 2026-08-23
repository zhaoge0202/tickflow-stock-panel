"""异动边缘监控 API — 按交易所异动规则口径统计接近触发的个股。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services.abnormal_moves import build_overview

router = APIRouter(prefix="/api/abnormal", tags=["abnormal"])


@router.get("/overview")
def abnormal_overview(
    request: Request,
    min_closeness: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(200, ge=1, le=1000),
):
    """异动边缘总览: 规则表 + 各窗口实时偏离 + 接近度排序。

    min_closeness: 0.5=观察 / 0.7=边缘 / 1.0=已触发。
    """
    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    return build_overview(repo, quote_service, min_closeness=min_closeness, limit=limit)
