"""市场广度 API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from app.services import market_breadth

router = APIRouter(prefix="/api/market-breadth", tags=["market-breadth"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


@router.get("/latest")
def latest(request: Request, force: bool = False):
    return market_breadth.safe_latest(_data_dir(request), force=force)
