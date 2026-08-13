from __future__ import annotations

import json

import pytest

from app.backtest.candidates import (
    CandidateStore,
    CandidateStoreError,
    CandidateValidationError,
)


def _create(store: CandidateStore):
    return store.create(
        kind="factor",
        name="20日动量候选",
        source_id="momentum_20d",
        config={"factor_name": "momentum_20d", "start": "2026-01-01"},
        metrics={"ic_mean": 0.04, "ir": 0.8},
        data_as_of="2026-08-11",
    )


def test_candidate_crud_and_atomic_file(tmp_path):
    store = CandidateStore(tmp_path)
    created = _create(store)

    assert store.path.exists()
    assert not store.path.with_suffix(".json.tmp").exists()
    assert store.list()[0]["id"] == created["id"]

    updated = store.update(created["id"], status="validated", name="动量候选 A")
    assert updated["status"] == "validated"
    assert store.list()[0]["name"] == "动量候选 A"

    store.delete(created["id"])
    assert store.list() == []


def test_candidate_rejects_full_result_fields(tmp_path):
    store = CandidateStore(tmp_path)

    with pytest.raises(CandidateValidationError, match="不允许的字段"):
        store.create(
            kind="strategy",
            name="策略候选",
            source_id="demo",
            config={"strategy_id": "demo", "equity_curve": [1, 2]},
            metrics={},
            data_as_of=None,
        )


def test_candidate_rejects_non_json_config(tmp_path):
    store = CandidateStore(tmp_path)

    with pytest.raises(CandidateValidationError, match="无法序列化"):
        store.create(
            kind="strategy",
            name="策略候选",
            source_id="demo",
            config={"strategy_id": object()},
            metrics={},
            data_as_of=None,
        )


def test_candidate_loads_legacy_missing_optional_fields(tmp_path):
    path = tmp_path / "user_data" / "research_candidates.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{
        "id": "legacy",
        "kind": "factor",
        "name": "旧候选",
        "config": {"factor_name": "rsi_14", "equity_curve": [1, 2]},
        "metrics": {"ic_mean": 0.03, "trades": [{"symbol": "000001.SZ"}]},
    }]), encoding="utf-8")

    item = CandidateStore(tmp_path).list()[0]
    assert item["source_id"] == "rsi_14"
    assert item["config"] == {"factor_name": "rsi_14"}
    assert item["metrics"] == {"ic_mean": 0.03}
    assert item["status"] == "pending"


def test_candidate_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "user_data" / "research_candidates.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    store = CandidateStore(tmp_path)

    with pytest.raises(CandidateStoreError, match="损坏"):
        store.list()
    with pytest.raises(CandidateStoreError, match="损坏"):
        _create(store)
    assert path.read_text(encoding="utf-8") == "{broken"
