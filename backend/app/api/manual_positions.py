"""手动持仓 API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import manual_positions

router = APIRouter(prefix="/api/manual-positions", tags=["manual-positions"])


class ManualPositionReq(BaseModel):
    symbol: str
    shares: float = 0
    cost_price: float = 0
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    target_position_pct: float | None = None
    note: str = ""


class ImportReq(BaseModel):
    positions: list[ManualPositionReq]


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _repo(request: Request):
    return request.app.state.repo


@router.get("")
def list_positions(request: Request):
    return {"positions": manual_positions.load_all(_data_dir(request), _repo(request))}


@router.put("/{symbol}")
def save_position(symbol: str, req: ManualPositionReq, request: Request):
    data = req.model_dump()
    data["symbol"] = symbol.upper()
    try:
        row = manual_positions.save_one(_data_dir(request), data, _repo(request))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "position": row}


@router.delete("/{symbol}")
def delete_position(symbol: str, request: Request):
    deleted = manual_positions.delete_one(_data_dir(request), symbol, _repo(request))
    if not deleted:
        raise HTTPException(status_code=404, detail="持仓不存在")
    return {"ok": True}


@router.post("/import")
def import_positions(req: ImportReq, request: Request):
    try:
        rows = manual_positions.import_many(_data_dir(request), [p.model_dump() for p in req.positions], _repo(request))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "positions": rows}
