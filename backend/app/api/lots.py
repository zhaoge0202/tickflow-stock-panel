"""批次登记 API — 薄"批次"页 (持仓提醒), 只做胶水, 不含会计语义。

映射/校验/持久化在 strategy.lots 域; 写完派生规则后复用 monitor_rules 的 _sync_engine 同步引擎。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import lots as lots_domain
from app.strategy import monitor_rules

router = APIRouter(prefix="/api/lots", tags=["lots"])

# 批次 + 派生规则 + 引擎重载的跨请求互斥; 规则全部校验通过才落盘, 避免半成品 (镜像 watchlist 服务层)。
_write_lock = threading.Lock()


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _resolve_asset_type(request: Request, symbol: str) -> str:
    """按 symbol 解析资产类型 (stock/etf); 解析失败默认 stock (fail-safe)。"""
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        # 回退为 stock 会让 etf 批次的止盈止损规则落入错误的监控轮, 必须留痕排查
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


class LotModel(BaseModel):
    id: str | None = None
    symbol: str
    qty: float = 0
    cost_price: float = 0
    buy_date: str | None = None
    target_pct: float = 0
    stop_pct: float = 0
    remind_date: str | None = None
    lead_days: int = 1


def _reload_engine(request: Request) -> None:
    """批次规则保存/删除后重载引擎 — 复用监控规则 API 的共享重载 (含指数纠正)。"""
    from app.api.monitor_rules import _sync_engine

    _sync_engine(request)


def sync_lot(request: Request, lot: dict) -> None:
    """写批次文件 + 同步其两条派生监控规则 + 重载引擎。

    派生规则继承用户默认推送渠道 (webhook_default_channels), 否则批次告警会静默只走应用内。
    """
    from app.services import preferences

    data_dir = _data_dir(request)
    with _write_lock:
        default_channels = preferences.get_webhook_default_channels()
        # ETF/指数等资产类型解析 (止盈止损价格规则须走对应资产监控轮才会触发)
        asset_type = _resolve_asset_type(request, lot["symbol"])
        price_rule, date_rule = lots_domain.lot_to_rules(lot)
        rules_to_write: list[dict] = []
        rules_to_delete: list[str] = []
        for rid, rule in ((f"{lot['id']}_p", price_rule), (f"{lot['id']}_d", date_rule)):
            if rule is None:
                rules_to_delete.append(rid)
                continue
            rule["asset_type"] = asset_type
            rule.setdefault("webhook_channels", list(default_channels))
            # 保留旧 created_at, 避免编辑批次后派生规则在监控中心列表跳位
            existing = monitor_rules.load_one(data_dir, rid)
            if existing and existing.get("created_at"):
                rule["created_at"] = existing["created_at"]
            try:
                monitor_rules.validate(rule)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            rules_to_write.append(monitor_rules.normalize(rule))
        lots_domain.save_one(data_dir, lot)
        for rid in rules_to_delete:
            monitor_rules.delete_one(data_dir, rid)
        for rule in rules_to_write:
            monitor_rules.save_one(data_dir, rule)
    _reload_engine(request)


@router.get("")
def list_lots(request: Request):
    return {"lots": lots_domain.load_all(_data_dir(request))}


@router.post("")
def upsert_lot(lot_in: LotModel, request: Request):
    """新建/更新一个批次。id 缺省时服务端生成 (紧凑, 保证 {id}_p/_d 规则 id ≤ 40 字符)。"""
    lot = lot_in.model_dump()
    if not lot.get("id"):
        lot["id"] = f"lot_{int(time.time() * 1000):x}_{secrets.token_hex(2)}"
    try:
        lots_domain.validate_lot(lot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    lot = lots_domain.normalize_lot(lot)
    sync_lot(request, lot)
    return {"ok": True, "lot": lot}


@router.delete("/{lot_id}")
def delete_lot(lot_id: str, request: Request):
    if not monitor_rules.ID_RE.match(lot_id):
        raise HTTPException(status_code=400, detail="批次 id 非法")
    data_dir = _data_dir(request)
    with _write_lock:
        deleted = lots_domain.delete_one(data_dir, lot_id)
        # 两条派生规则都要删 (用 or 会短路跳过第二条)
        deleted_p = monitor_rules.delete_one(data_dir, f"{lot_id}_p")
        deleted_d = monitor_rules.delete_one(data_dir, f"{lot_id}_d")
    if deleted or deleted_p or deleted_d:
        _reload_engine(request)
    return {"ok": True}
