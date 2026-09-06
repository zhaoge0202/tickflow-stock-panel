"""复盘推送触发方式 review_push_mode 测试 — auto/manual 白名单与默认值 + 推送门控。"""
from __future__ import annotations

import asyncio

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def test_review_push_mode_defaults_to_manual():
    assert preferences.get_review_push_mode() == "manual"


def test_set_and_get_review_push_mode():
    assert preferences.set_review_push_mode("auto") == "auto"
    assert preferences.get_review_push_mode() == "auto"

    assert preferences.set_review_push_mode("manual") == "manual"
    assert preferences.get_review_push_mode() == "manual"


def test_set_review_push_mode_rejects_invalid_value():
    assert preferences.set_review_push_mode("bogus") == "manual"
    assert preferences.get_review_push_mode() == "manual"


# ── 推送门控 ────────────────────────────────────────────────────────
# 门控语义:
#   manual: 定时复盘只归档不推送; 手动保存需显式 push=True 才推
#   auto:   归档即推(与旧逻辑一致)

def test_save_report_manual_requires_explicit_push(monkeypatch):
    from app.api import market_recap
    from app.jobs import daily_pipeline

    pushed: list[dict] = []
    monkeypatch.setattr(
        "app.services.market_recap_reports.save_report",
        lambda d: {"id": "r1"},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_maybe_push_review",
        lambda content, meta: pushed.append(meta),
    )
    preferences.set_review_push_mode("manual")

    # 默认 push=False: manual 模式下只归档, 不外发
    market_recap.save_report(None, market_recap.SaveReportRequest(as_of="2026-07-18", content="正文"))
    assert pushed == []

    # 显式 push=True: manual 模式下外发
    market_recap.save_report(None, market_recap.SaveReportRequest(as_of="2026-07-18", content="正文", push=True))
    assert pushed == [{"as_of": "2026-07-18", "emotion_label": ""}]


def test_save_report_auto_pushes_without_flag(monkeypatch):
    from app.api import market_recap
    from app.jobs import daily_pipeline

    pushed: list[dict] = []
    monkeypatch.setattr(
        "app.services.market_recap_reports.save_report",
        lambda d: {"id": "r1"},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_maybe_push_review",
        lambda content, meta: pushed.append(meta),
    )
    preferences.set_review_push_mode("auto")

    # auto 模式: 无需 push 标志即外发
    market_recap.save_report(None, market_recap.SaveReportRequest(as_of="2026-07-18", content="正文"))
    assert pushed == [{"as_of": "2026-07-18", "emotion_label": ""}]


def _patch_scheduled_review(monkeypatch, pushed: list, archived: list):
    """装配定时复盘的依赖: 有 AI key、流式产出固定内容、捕获归档与推送调用。"""
    from app.jobs import daily_pipeline

    async def _fake_stream(*a, **k):
        return "正文", {"as_of": "2026-07-18", "emotion_label": "中性"}

    monkeypatch.setattr("app.secrets_store.get_ai_key", lambda: "sk-test")
    monkeypatch.setattr(daily_pipeline, "_stream_review_with_retry", _fake_stream)
    monkeypatch.setattr(
        "app.services.market_recap_reports.save_report",
        lambda d: archived.append(d) or {"id": "r1"},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_maybe_push_review",
        lambda content, meta: pushed.append(meta),
    )


def test_scheduled_review_manual_archives_without_push(monkeypatch):
    from app.jobs import daily_pipeline

    pushed: list = []
    archived: list = []
    _patch_scheduled_review(monkeypatch, pushed, archived)
    preferences.set_review_push_mode("manual")

    asyncio.run(daily_pipeline._run_scheduled_review(None))

    # manual 模式: 归档发生, 但不外发
    assert len(archived) == 1
    assert pushed == []


def test_scheduled_review_auto_pushes(monkeypatch):
    from app.jobs import daily_pipeline

    pushed: list = []
    archived: list = []
    _patch_scheduled_review(monkeypatch, pushed, archived)
    preferences.set_review_push_mode("auto")

    asyncio.run(daily_pipeline._run_scheduled_review(None))

    # auto 模式: 归档并外发
    assert len(archived) == 1
    assert pushed == [{"as_of": "2026-07-18", "emotion_label": "中性"}]
