"""策略页用户买入标记 API。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import strategy_purchase_marks
from app.services.symbols import normalize_symbol

router = APIRouter(prefix="/api/strategy-purchase-marks", tags=["strategy-purchase-marks"])


class PurchaseMarkReq(BaseModel):
    strategy_id: str
    strategy_name: str = ""
    symbol: str
    signal_date: date
    signal_price: float | None = None
    signal_score: float | None = None
    signal_change_pct: float | None = None
    note: str = ""


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


@router.get("")
def list_marks(
    request: Request,
    strategy_id: str | None = None,
    signal_date: date | None = None,
):
    rows = strategy_purchase_marks.load_all(_data_dir(request))
    if strategy_id:
        rows = [row for row in rows if row.get("strategy_id") == strategy_id]
    if signal_date:
        target = signal_date.isoformat()
        rows = [row for row in rows if row.get("signal_date") == target]
    return {"marks": rows}


@router.put("")
def save_mark(req: PurchaseMarkReq, request: Request):
    try:
        row = req.model_dump()
        row["symbol"] = normalize_symbol(req.symbol, request.app.state.repo)
        row["signal_date"] = req.signal_date.isoformat()
        saved = strategy_purchase_marks.save_one(_data_dir(request), row)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "mark": saved}


@router.delete("")
def delete_mark(
    request: Request,
    strategy_id: str,
    symbol: str,
    signal_date: date,
):
    normalized = normalize_symbol(symbol, request.app.state.repo)
    deleted = strategy_purchase_marks.delete_one(
        _data_dir(request),
        strategy_id,
        normalized,
        signal_date.isoformat(),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="买入标记不存在")
    return {"ok": True}
