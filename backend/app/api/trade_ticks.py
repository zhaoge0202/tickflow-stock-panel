"""逐笔成交 API。"""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.plugins.tdxapi.provider import TDXAPIProvider
from app.services.trade_tick_ingest import trade_tick_ingestor
from app.services.trade_tick_mysql import CREATE_TABLE_SQL, trade_tick_mysql_store

router = APIRouter(prefix="/api/trade-ticks", tags=["trade-ticks"])


class PersistReq(BaseModel):
    symbol: str
    date: dt.date | None = None
    force: bool = False


@router.get("")
def list_trade_ticks(
    symbol: Annotated[str, Query(description="标的代码, 如 000001.SZ")],
    trade_date: Annotated[dt.date | None, Query(alias="date", description="交易日期, 默认今天")] = None,
    source: Annotated[Literal["auto", "live", "mysql"], Query()] = "auto",
    mode: Annotated[
        Literal["recent", "all"],
        Query(description="live 模式下 recent=最近成交 all=当日全量"),
    ] = "recent",
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
):
    """查询逐笔成交。

    - live: 直接查 tdx-api sidecar
    - mysql: 查本地 MySQL 持久化数据
    - auto: 历史日期优先 MySQL; 今天优先 live, live 失败时回退 MySQL
    """
    symbol = symbol.strip().upper()
    day = trade_date or dt.date.today()

    if source == "mysql":
        rows = _mysql_rows(symbol, day, limit, order)
        return _response(symbol, day, "mysql", mode, rows, order)

    if source == "auto" and day < dt.date.today() and trade_tick_mysql_store.configured():
        try:
            rows = _mysql_rows(symbol, day, limit, order)
            if rows:
                return _response(symbol, day, "mysql", mode, rows, order)
        except Exception:
            pass

    try:
        rows = _live_rows(symbol, day, mode, limit, order)
        return _response(symbol, day, "tdxapi", mode, rows, order)
    except Exception as e:
        if source == "live":
            raise HTTPException(status_code=502, detail=f"TDX 逐笔成交拉取失败: {e}") from e
        if trade_tick_mysql_store.configured():
            try:
                rows = _mysql_rows(symbol, day, limit, order)
                return _response(symbol, day, "mysql_fallback", mode, rows, order, warning=str(e))
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"TDX 逐笔成交拉取失败: {e}") from e


@router.get("/auction-result")
def get_auction_result(
    symbol: Annotated[str, Query(description="标的代码, 如 000001.SZ")],
    trade_date: Annotated[dt.date, Query(alias="date", description="交易日期")],
):
    """查询指定交易日 09:25 开盘竞价结果。

    数据来自 TDX 历史分笔成交的 09:25 行, 不是竞价过程明细。
    """
    symbol = symbol.strip().upper()
    provider = TDXAPIProvider()
    try:
        rows = provider.get_auction_results([symbol], trade_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TDX 竞价结果拉取失败: {e}") from e
    finally:
        provider.close()
    rows = sorted(rows, key=lambda row: int(row.get("trade_index") or 0))
    return {
        "symbol": symbol,
        "date": trade_date.isoformat(),
        "source": "tdxapi",
        "kind": "opening_auction_result",
        "time": "09:25",
        "process_available": False,
        "process_note": "本接口只返回 09:25 最终成交结果; 竞价过程仍依赖实时采集。",
        "count": len(rows),
        "rows": rows,
    }


@router.post("/persist")
def persist_trade_ticks(req: PersistReq):
    return trade_tick_ingestor.enqueue(req.symbol, req.date, force=req.force)


@router.get("/persist-status")
def persist_status(
    symbol: Annotated[str, Query()],
    trade_date: Annotated[dt.date | None, Query(alias="date")] = None,
):
    return trade_tick_ingestor.status(symbol, trade_date)


@router.get("/mysql/status")
def mysql_status():
    return trade_tick_mysql_store.health()


@router.get("/mysql/schema")
def mysql_schema():
    """返回建表 SQL, 供用户确认后手动或通过运维脚本执行。"""
    return {"table": "trade_ticks", "sql": CREATE_TABLE_SQL}


def _live_rows(symbol: str, day: dt.date, mode: str, limit: int, order: str) -> list[dict]:
    provider = TDXAPIProvider()
    try:
        rows = provider.get_trade_ticks(symbol, day, mode=mode, limit=limit)
    finally:
        provider.close()
    return _sort_rows(rows, order)


def _mysql_rows(symbol: str, day: dt.date, limit: int, order: str) -> list[dict]:
    try:
        return trade_tick_mysql_store.list_ticks(symbol, day, limit=limit, order=order)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MySQL 逐笔成交查询失败: {e}") from e


def _sort_rows(rows: list[dict], order: str) -> list[dict]:
    reverse = order != "asc"
    return sorted(rows, key=lambda r: int(r.get("seq_in_day") or 0), reverse=reverse)


def _response(
    symbol: str,
    day: dt.date,
    source: str,
    mode: str,
    rows: list[dict],
    order: str,
    warning: str | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "date": day.isoformat(),
        "source": source,
        "mode": mode,
        "order": order,
        "time_precision": "minute",
        "sequence_field": "seq_in_day",
        "count": len(rows),
        "rows": rows,
        "warning": warning,
    }
