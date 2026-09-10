"""自动挖掘 L1 筛选 (auto_mining) 与 /api/backtest/mining/auto 契约测试。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app.services import auto_mining
from app.services.auto_mining import (
    SCREEN_GATES,
    classify_factor,
    screen_all_factors,
)

# ── classify_factor: 门槛分支 ──


def _item(**overrides: Any) -> dict[str, Any]:
    base = {"error": None, "ic_mean": 0.05, "ir": 0.4, "t_newey_west": 2.5, "q_value": 0.05}
    base.update(overrides)
    return base


def test_classify_balanced_gate_branches() -> None:
    gate = SCREEN_GATES["balanced"]
    assert classify_factor(_item(), gate) is None
    assert "计算失败" in classify_factor(_item(error="boom"), gate)
    assert "样本不足" in classify_factor(_item(ic_mean=None), gate)
    assert "预测力弱" in classify_factor(_item(ic_mean=0.01), gate)
    assert "稳定度低" in classify_factor(_item(ic_mean=0.05, ir=0.1), gate)
    assert "NW t" in classify_factor(_item(t_newey_west=None), gate)
    assert "不显著" in classify_factor(_item(t_newey_west=1.0), gate)
    assert "多重检验" in classify_factor(_item(q_value=0.5), gate)
    # q 缺失按通过 (探索档小样本口径)
    assert classify_factor(_item(q_value=None), gate) is None


def test_classify_profile_gates_tighten() -> None:
    item = _item(ic_mean=-0.025, ir=-0.35, t_newey_west=-2.2, q_value=0.08)
    # 负值同样达标 (方向反向), 严格档收紧后不达标
    assert classify_factor(item, SCREEN_GATES["balanced"]) is None
    assert classify_factor(item, SCREEN_GATES["strict"]) is not None
    # 探索档最宽
    weak = _item(ic_mean=0.021, ir=0.16, t_newey_west=1.6, q_value=0.18)
    assert classify_factor(weak, SCREEN_GATES["exploratory"]) is None
    assert classify_factor(weak, SCREEN_GATES["balanced"]) is not None


# ── screen_all_factors: 池构造/排序/截断/清洗 ──


class _StubService:
    calls: ClassVar[list[Any]] = []
    results: ClassVar[list[Any]] = []

    def run_batch(self, config: Any) -> Any:
        _StubService.calls.append(config)
        return SimpleNamespace(results=_StubService.results)


def _batch_item(name: str, **overrides: Any) -> SimpleNamespace:
    base = {
        "factor_name": name, "label": f"label_{name}", "group": "g",
        "ic_mean": 0.05, "ir": 0.4, "t_newey_west": 2.5, "q_value": 0.05,
        "error": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def _stub_batch(monkeypatch: pytest.MonkeyPatch) -> type[_StubService]:
    _StubService.calls = []
    monkeypatch.setattr(auto_mining, "FactorBacktestService", lambda engine: _StubService())
    return _StubService


def test_screen_pool_order_truncation_and_reasons(monkeypatch: pytest.MonkeyPatch, _stub_batch: type[_StubService]) -> None:
    monkeypatch.setattr(auto_mining, "factor_columns_view", lambda: [
        {"id": name, "asset_types": ["stock"]} for name in ("a", "b", "c", "d", "e", "f")
    ])
    items = [
        _batch_item("a", ic_mean=0.10, ir=0.8),   # |ic|*|ir|=0.08 → 第 1
        _batch_item("b", ic_mean=0.05, ir=0.4),   # 0.02 → 第 2
        _batch_item("c", ic_mean=-0.06, ir=-0.5), # 0.03 → 第 3 (负 IC 反向)
        _batch_item("d", ic_mean=0.01),           # 预测力弱
        _batch_item("e", ir=0.1),                 # 稳定度低
        _batch_item("f", t_newey_west=float("nan")),  # NaN 清洗 → 样本不足
    ]
    _stub_batch.results = items
    summary = screen_all_factors(
        object(), asset_type="stock",
        start=date.today() - timedelta(days=800),
        end=date.today(), profile="balanced",
        max_factors=2,
    )
    assert summary["pool"] == ["a", "c"]  # 0.08 > 0.03 > 0.02, 截断到 2
    assert summary["pool_truncated"] is True
    assert summary["n_qualified"] == 3
    assert summary["n_total"] == len(summary["qualified"]) + len(summary["failed"])
    assert {row["factor_name"]: row["direction"] for row in summary["qualified"]}["c"] == -1
    # NaN 指标被清洗为 None, 归入样本不足而非写入非法 JSON
    row_f = next(row for row in summary["failed"] if row["factor_name"] == "f")
    assert row_f["t"] is None and "样本不足" in row_f["reason"]
    counts = summary["reason_counts"]
    assert counts["预测力弱"] == 1
    assert counts["稳定度低"] == 1
    assert any(key.startswith("样本不足") for key in counts)
    assert sum(counts.values()) == 3


def test_screen_window_capped_and_daily_rebalance(monkeypatch: pytest.MonkeyPatch, _stub_batch: type[_StubService]) -> None:
    _stub_batch.results = []
    start = date.today() - timedelta(days=1000)
    screen_all_factors(
        object(), asset_type="stock", start=start, end=date.today(),
        profile="exploratory",
    )
    config = _stub_batch.calls[0]
    assert config.rebalance == "daily"
    assert (config.end - config.start).days <= auto_mining.SCREEN_WINDOW_DAYS
    # 因子清单来自注册表 (动态视图, 数量与内置一致量级)
    assert len(config.factor_names) >= 50


# ── API 契约: /api/backtest/mining/auto ──


class _FakeManager:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.start_calls: list[dict[str, Any]] = []

    def start(self, request, fingerprint, force=False, source="manual", run_id=None):
        self.start_calls.append({"request": request, "fingerprint": fingerprint,
                                 "force": force, "source": source})
        return self.store.create(request, fingerprint)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import mining as mining_api
    from app.services.mining_jobs import MiningRunStore

    store = MiningRunStore(tmp_path / "runs")
    manager = _FakeManager(store)

    app = FastAPI()
    app.include_router(mining_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.mining_manager = manager

    monkeypatch.setattr(mining_api, "require_mining_availability", lambda *a, **k: None)
    monkeypatch.setattr(mining_api, "enriched_partition_dates", lambda *a, **k: ["2026-08-31"])
    monkeypatch.setattr(mining_api, "build_data_fingerprint", lambda *a, **k: {"fp": 1})
    return SimpleNamespace(client=TestClient(app), manager=manager, store=store)


def _screening(pool: list[str]) -> dict[str, Any]:
    return {
        "profile": "balanced", "gate": {"min_abs_ic": 0.02}, "n_total": 61,
        "n_qualified": len(pool), "pool": pool, "pool_truncated": False,
        "qualified": [], "failed": [], "reason_counts": {},
        "screen_window": {"start": "2025-08-31", "end": "2026-08-31"},
        "elapsed_ms": 1.0,
    }


def test_auto_start_contract_with_pool(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auto_mining, "screen_all_factors",
        lambda *a, **k: _screening(["f1", "f2"]),
    )
    response = client.client.post("/api/backtest/mining/auto", json={"asset_type": "stock"})
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["run"]["status"] == "queued"
    call = client.manager.start_calls[0]
    assert call["source"] == "auto"
    request = call["request"]
    assert request["factor_names"] == ["f1", "f2"]
    assert request["strategy_ids"] == []
    assert request["auto"] is True
    assert request["auto_screening"]["pool"] == ["f1", "f2"]
    # 持久化 roundtrip: 任务存储里的 request 保留筛选摘要, 供结果页展示
    manifest = client.store.get(body["run"]["run_id"])
    assert manifest is not None
    assert manifest["request"]["auto_screening"]["n_total"] == 61


def test_auto_start_no_qualified_factors(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auto_mining, "screen_all_factors",
        lambda *a, **k: _screening([]),
    )
    response = client.client.post("/api/backtest/mining/auto", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is False
    assert body["reason"] == "no_qualified_factors"
    assert client.manager.start_calls == []


def test_auto_start_rejects_bad_date_range(client: Any) -> None:
    response = client.client.post("/api/backtest/mining/auto", json={
        "start": "2026-09-01", "end": "2026-08-01",
    })
    assert response.status_code == 422
