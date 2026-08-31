"""API 路由 — Phase 0 仅 /health 与 /api/capabilities。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities, tier_label

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        # 三态: none(无key/无效) / free(免费key) / api_key(付费档)
        "mode": tf_client.current_mode(),
    }


@router.get("/api/capabilities")
def capabilities() -> dict:
    """前端用来决定哪些功能可用、哪些灰显。"""
    capset = detect_capabilities()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """用户在设置页"重新检测"按钮。"""
    capset = detect_capabilities(force=True)
    # 同步刷新 app.state 快照 (minute_refresh 等服务的门控读这里) 与财务调度器,
    # 与 settings.py 各探测路径一致 — 否则重检测后服务侧仍读旧 capset 被错误门控
    request.app.state.capabilities = capset
    from app.api.settings import _sync_financial_scheduler_caps
    _sync_financial_scheduler_caps(request.app.state, capset)
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }
