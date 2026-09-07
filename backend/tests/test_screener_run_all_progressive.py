"""run_all 渐进式返回的端点行为 — 快策略先返回、慢策略后台落缓存、搭车与旧路径兼容。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import screener as screener_api
from app.config import settings
from app.services import strategy_cache, strategy_run_queue

AS_OF = "2026-09-04"


@dataclass
class _FakeResult:
    total: int = 1
    rows: list = field(default_factory=lambda: [{"symbol": "000001.SZ", "close": 1.0}])
    as_of: str = AS_OF


class _FakeEngine:
    def __init__(self, delays: dict[str, float]):
        self._delays = delays
        self.executed: list[str] = []

    def has(self, sid: str) -> bool:
        return sid in self._delays

    def get(self, sid: str):
        return SimpleNamespace(meta={})

    def run_all(self, context, params_map=None, overrides_map=None, *, strategy_ids=None, parallel=True):
        out = {}
        for sid in strategy_ids or []:
            self.executed.append(sid)
            time.sleep(self._delays[sid])
            out[sid] = _FakeResult()
        return out


class _FakeService:
    def __init__(self, repo, asset_type="stock"):
        pass

    def latest_date(self):
        return date.fromisoformat(AS_OF)

    def build_strategy_context(self, *args, **kwargs):
        return SimpleNamespace()


def _request(tmp_path, engine):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, strategy_engine=engine, monitor_engine=None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _wait_cache_results(tmp_path, want_ids, timeout=8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        results = (strategy_cache.read_cache(tmp_path) or {}).get("results") or {}
        if all(i in results for i in want_ids):
            return results
        time.sleep(0.05)
    return (strategy_cache.read_cache(tmp_path) or {}).get("results") or {}


@pytest.fixture()
def fast_first_return(monkeypatch):
    monkeypatch.setattr(settings, "strategy_run_all_first_return_s", 0.4)


def test_run_all_returns_fast_first_then_background_fills_cache(
    monkeypatch, tmp_path, fast_first_return
):
    engine = _FakeEngine({"fast_a": 0.02, "fast_b": 0.02, "slow_c": 0.8})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)

    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["fast_a", "fast_b", "slow_c"],
            "asset_type": "stock",
            "timeframe": "1d",
            "summary_only": True,
        },
    )
    # 首返: 快策略已完成 (带 total, 无 rows), 慢策略 pending
    assert set(resp["results"]) == {"fast_a", "fast_b"}
    assert resp["results"]["fast_a"]["total"] == 1
    assert "rows" not in resp["results"]["fast_a"]
    assert resp["pending"] == ["slow_c"]
    assert resp["complete"] is False
    assert isinstance(resp["started_at"], int)

    # 后台继续: 慢策略最终也落进缓存, 且带 computed_at
    results = _wait_cache_results(tmp_path, ["fast_a", "fast_b", "slow_c"])
    assert set(results) == {"fast_a", "fast_b", "slow_c"}
    assert all(r.get("computed_at") for r in results.values())

    # 耗时已记录 → 下次按耗时升序 (快策略先算)
    timings = strategy_run_queue.load_run_timings(tmp_path)
    assert set(timings) == {"fast_a", "fast_b", "slow_c"}
    assert timings["slow_c"] > timings["fast_a"]


def test_run_all_second_run_orders_by_recorded_timings(
    monkeypatch, tmp_path, fast_first_return
):
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    engine = _FakeEngine({"slow_a": 0.5, "fast_b": 0.01})
    body = {
        "as_of": AS_OF,
        "strategy_ids": ["slow_a", "fast_b"],
        "asset_type": "stock",
        "timeframe": "1d",
        "summary_only": True,
    }
    req = _request(tmp_path, engine)

    def _wait_executed(count, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(engine.executed) >= count:
                return
            time.sleep(0.05)
        raise AssertionError(f"执行未到 {count} 个: {engine.executed}")

    # 第一轮: 无耗时记录 → 按传入顺序 slow_a 先算
    screener_api.run_all(req, body)
    _wait_executed(2)
    assert engine.executed == ["slow_a", "fast_b"]

    # 第二轮: 有耗时记录 → fast_b (快) 升序在前, 且首返带上 fast_b。
    # 若第一轮 handle 还在收尾 (终写缓存/记录耗时), 请求会搭车旧 handle;
    # 重试直到真正触发新一轮执行。
    resp2 = None
    for _ in range(40):
        engine.executed.clear()
        resp2 = screener_api.run_all(req, body)
        if engine.executed:
            break
        time.sleep(0.05)
    assert resp2 is not None and engine.executed, "第二轮未触发新执行"
    assert engine.executed[0] == "fast_b"
    assert set(resp2["results"]) == {"fast_b"}
    _wait_executed(2)
    assert engine.executed == ["fast_b", "slow_a"]


def test_run_all_same_key_piggybacks_running_execution(
    monkeypatch, tmp_path, fast_first_return
):
    engine = _FakeEngine({"fast_a": 0.02, "slow_c": 1.2})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    body = {
        "as_of": AS_OF,
        "strategy_ids": ["fast_a", "slow_c"],
        "asset_type": "stock",
        "timeframe": "1d",
        "summary_only": True,
    }
    req = _request(tmp_path, engine)
    resp1 = screener_api.run_all(req, body)
    # 第一笔仍在后台跑 slow_c 时, 相同请求搭车: 同一起点, 不重复执行
    resp2 = screener_api.run_all(_request(tmp_path, engine), body)
    assert resp2["started_at"] == resp1["started_at"]
    assert engine.executed.count("fast_a") == 1
    _wait_cache_results(tmp_path, ["fast_a", "slow_c"])
    assert engine.executed.count("slow_c") == 1
    assert engine.executed.count("fast_a") == 1


def test_run_all_background_error_without_results_is_500(
    monkeypatch, tmp_path, fast_first_return
):
    class _BoomEngine(_FakeEngine):
        def run_all(self, context, params_map=None, overrides_map=None, *, strategy_ids=None, parallel=True):
            raise ValueError("缺少列: volume")

    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    with pytest.raises(HTTPException) as excinfo:
        screener_api.run_all(
            _request(tmp_path, _BoomEngine({"bad_a": 0.01})),
            {
                "as_of": AS_OF,
                "strategy_ids": ["bad_a"],
                "asset_type": "stock",
                "timeframe": "1d",
                "summary_only": True,
            },
        )
    assert excinfo.value.status_code == 500
    assert "volume" in excinfo.value.detail


def test_run_all_minute_timeframe_stays_blocking(monkeypatch, tmp_path, fast_first_return):
    engine = _FakeEngine({"m1": 0.01})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["m1"],
            "asset_type": "stock",
            "timeframe": "1m",
            "summary_only": True,
        },
    )
    # 分钟周期: 整段阻塞、旧响应形状 (无 pending), 且不写日线缓存
    assert set(resp["results"]) == {"m1"}
    assert "pending" not in resp
    assert not ((strategy_cache.read_cache(tmp_path) or {}).get("results") or {})


def test_run_all_full_detail_stays_blocking(monkeypatch, tmp_path, fast_first_return):
    engine = _FakeEngine({"d1": 0.01})
    monkeypatch.setattr(screener_api, "ScreenerService", _FakeService)
    resp = screener_api.run_all(
        _request(tmp_path, engine),
        {
            "as_of": AS_OF,
            "strategy_ids": ["d1"],
            "asset_type": "stock",
            "timeframe": "1d",
        },
    )
    # 非 summary 请求: 保持整段阻塞并返回明细
    assert resp["results"]["d1"]["rows"][0]["symbol"] == "000001.SZ"
    assert "pending" not in resp
