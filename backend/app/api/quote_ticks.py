"""秒级行情事实 API。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Query, Request

from app.services import quote_tick_store
from app.services.quote_snapshot_mysql import quote_snapshot_mysql_store

router = APIRouter(prefix="/api/quote-ticks", tags=["quote-ticks"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.replace(";", ",").split(",") if item.strip()]


@router.get("/latest")
def latest(request: Request, symbols: str | None = None):
    rows = quote_tick_store.latest(_data_dir(request), _symbols(symbols))
    return {"rows": rows, "count": len(rows)}


@router.get("/bars")
def bars(
    request: Request,
    symbol: str,
    freq: str = "5s",
    date_: str | None = Query(default=None, alias="date"),
):
    target_date = date.fromisoformat(date_) if date_ else None
    rows = quote_tick_store.bars(_data_dir(request), symbol.upper(), freq=freq, target_date=target_date)
    return {"symbol": symbol.upper(), "freq": freq, "rows": rows, "count": len(rows)}


@router.get("/quality")
def quality(request: Request, symbols: str | None = None):
    return quote_tick_store.quality(_data_dir(request), _symbols(symbols))


@router.get("/mysql/status")
def mysql_status():
    """返回最新行情 MySQL 热缓存的配置和表状态。"""
    return quote_snapshot_mysql_store.health()
