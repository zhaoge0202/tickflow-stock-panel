"""SignalFrame API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.services import signal_frame

router = APIRouter(prefix="/api/signal-frame", tags=["signal-frame"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.replace(";", ",").split(",") if item.strip()]


@router.get("/latest")
def latest(request: Request, symbols: str | None = None):
    rows = signal_frame.build_latest_frames(
        _data_dir(request),
        request.app.state.repo,
        symbols=_symbols(symbols),
        include_levels=True,
    )
    return {"frames": rows, "count": len(rows)}


@router.get("/detail/{symbol}")
def detail(symbol: str, request: Request):
    frame = signal_frame.build_detail(_data_dir(request), request.app.state.repo, symbol.upper())
    if frame is None:
        raise HTTPException(status_code=404, detail="SignalFrame 不存在")
    return frame
