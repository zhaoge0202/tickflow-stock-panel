"""run_all 的 as_of 入参口径 — 与 /custom、/preset 的 `as_of: date` 契约对齐。

/api/screener/custom 与 /api/screener/preset 走 Pydantic 模型 (`as_of: Optional[date]`),
非法日期在入口被拦下; run_all 收的是无类型 dict, 同样的入参会漏进业务层。
本文件锁住三条: 非法字符串被拦、非日期类型被拦、合法日期照常放行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import screener as screener_api
from app.services import strategy_cache

AS_OF = "2026-09-04"


@dataclass
class _FakeResult:
    total: int = 1
    rows: list = field(default_factory=lambda: [{"symbol": "000001.SZ", "close": 1.0}])
    as_of: str = AS_OF


class _FakeEngine:
    def has(self, sid: str) -> bool:
        return sid == "s1"

    def get(self, sid: str):
        return SimpleNamespace(meta={})

    def run_all(self, context, params_map=None, overrides_map=None, *, strategy_ids=None, parallel=True):
        return {sid: _FakeResult() for sid in (strategy_ids or [])}


class _FakeService:
    def __init__(self, repo, asset_type="stock"):
        pass

    def latest_date(self):
        return date.fromisoformat(AS_OF)

    def build_strategy_context(self, *args, **kwargs):
        return SimpleNamespace()


def _request(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, strategy_engine=_FakeEngine(), monitor_engine=None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _body(as_of):
    return {
        "as_of": as_of,
        "strategy_ids": ["s1"],
        "asset_type": "stock",
        "timeframe": "1d",
    }


@pytest.fixture(autouse=True)
def _fake_service(monkeypatch):
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-01", "2026/09/04"])
def test_malformed_as_of_string_is_rejected(tmp_path, bad):
    with pytest.raises(HTTPException) as excinfo:
        screener_api.run_all(_request(tmp_path), _body(bad))
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize("bad", [20260904, 2026, 1.5, True, ["2026-09-04"]])
def test_non_string_as_of_is_rejected_and_never_reaches_cache(tmp_path, bad):
    """JSON 数字等非字符串 as_of 不得被当成日期用作缓存键。

    /preset 的 `as_of: date` 对 int 报 422 (date_from_datetime_inexact),
    run_all 必须同样拦下, 否则 str(as_of) 会把 "20260904" 之类写进 strategy_cache.json,
    与其它入口写的 "2026-09-04" 不是同一口径, 缓存按 as_of 比对时永远失配。
    """
    with pytest.raises(HTTPException) as excinfo:
        screener_api.run_all(_request(tmp_path), _body(bad))
    assert excinfo.value.status_code == 400
    assert strategy_cache.read_cache(tmp_path) in (None, {}) or not (
        strategy_cache.read_cache(tmp_path) or {}
    ).get("results")


def test_valid_as_of_still_runs_and_writes_iso_cache(tmp_path):
    resp = screener_api.run_all(_request(tmp_path), _body(AS_OF))
    assert resp["as_of"] == AS_OF
    assert resp["results"]["s1"]["total"] == 1
    assert (strategy_cache.read_cache(tmp_path) or {}).get("as_of") == AS_OF


def test_missing_as_of_falls_back_to_latest_date(tmp_path):
    resp = screener_api.run_all(_request(tmp_path), _body(None))
    assert resp["as_of"] == AS_OF
