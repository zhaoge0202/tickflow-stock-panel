"""盘中提醒回放 API。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import auction_replay, strategy_cache

router = APIRouter(prefix="/api/replay", tags=["replay"])


class IntradayReplayReq(BaseModel):
    date: date
    symbols: list[str]
    start_time: str | None = None
    end_time: str | None = None


class AuctionReplayReq(BaseModel):
    as_of: date | None = None
    trade_date: date | None = None
    strategy_ids: list[str] | None = None
    as_of_ts: int | None = None
    include_frames: bool = True
    include_candidates: bool = False
    max_frames: int = 600
    mode: str = "cache_replay"
    asset_type: str = "stock"
    timeframe: str = "1d"


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _cached_with_realtime(request: Request) -> dict:
    data_dir = _data_dir(request)
    cached = strategy_cache.read_cache(data_dir)
    if cached is None:
        cached = {"as_of": None, "results": {}, "updated_at": None}

    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    if monitor_engine is not None:
        realtime_results = monitor_engine.latest_strategy_results()
        if realtime_results:
            results = dict(cached.get("results") or {})
            results.update(realtime_results)
            cached = dict(cached)
            cached["results"] = results
    return cached


@router.post("/intraday")
def run_intraday(req: IntradayReplayReq, request: Request):
    from app.services import intraday_replay

    symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    return intraday_replay.enqueue_replay(
        _data_dir(request),
        target_date=req.date,
        symbols=symbols,
        start_time=req.start_time,
        end_time=req.end_time,
    )


@router.get("/intraday/{task_id}")
def get_intraday(task_id: str):
    from app.services import intraday_replay

    task = intraday_replay.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="回放任务不存在")
    return task


@router.post("/auction")
def run_auction(req: AuctionReplayReq, request: Request):
    """逐秒回放 09:23-09:30 的竞价确认过程。"""
    if req.mode in {"recompute", "dynamic", "auction_dynamic"}:
        engine = getattr(request.app.state, "strategy_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="策略引擎未初始化")
        unknown = [
            sid for sid in (req.strategy_ids or [])
            if sid and not engine.has(str(sid))
        ]
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown strategies: {unknown}")
        return auction_replay.replay_dynamic_strategy_results(
            request.app.state.repo,
            engine,
            as_of=req.as_of,
            trade_date=req.trade_date,
            strategy_ids=req.strategy_ids,
            as_of_ts=req.as_of_ts,
            include_frames=req.include_frames,
            include_candidates=req.include_candidates,
            max_frames=req.max_frames,
            asset_type=req.asset_type,
            timeframe=req.timeframe,
        )
    return auction_replay.replay_cached_strategy_results(
        _data_dir(request),
        _cached_with_realtime(request),
        as_of=req.as_of,
        trade_date=req.trade_date,
        strategy_ids=req.strategy_ids,
        as_of_ts=req.as_of_ts,
        include_frames=req.include_frames,
        include_candidates=req.include_candidates,
        max_frames=req.max_frames,
    )
