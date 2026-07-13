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


def _business_capabilities(request: Request) -> dict:
    depth_svc = getattr(request.app.state, "depth_service", None)
    if depth_svc:
        sealed_depth = depth_svc.capability_status()
    else:
        from app.services.depth_service import DepthService
        sealed_depth = DepthService().capability_status()
    return {"sealed_depth": sealed_depth}


@router.get("/api/capabilities")
def capabilities(request: Request) -> dict:
    """前端用来决定哪些功能可用、哪些灰显。"""
    capset = detect_capabilities()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
        "business_capabilities": _business_capabilities(request),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """用户在设置页"重新检测"按钮。"""
    capset = detect_capabilities(force=True)
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
        "business_capabilities": _business_capabilities(request),
    }
