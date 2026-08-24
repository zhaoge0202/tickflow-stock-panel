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


def test_candidate_mining_provenance_is_idempotent_and_conflict_safe(tmp_path):
    store = CandidateStore(tmp_path)
    kwargs = {
        "origin_run_id": "mining-run-idempotent",
        "candidate_signature": "factor-signature",
        "kind": "strategy",
        "name": "挖掘组合候选",
        "source_id": "mined_factor_example",
        "config": {
            "strategy_id": "mined_factor_example",
            "origin_run_id": "mining-run-idempotent",
            "candidate_signature": "factor-signature",
            "factor_names": ["turnover_rate"],
            "directions": ["high"],
            "weights": [1.0],
        },
        "metrics": {"oos_sharpe": 0.9},
        "data_as_of": "2026-08-11",
    }

    first = store.create_or_get_by_provenance(**kwargs)
    second = store.create_or_get_by_provenance(**kwargs)

    assert second == first
    assert len(store.list()) == 1
    with pytest.raises(CandidateValidationError, match="冲突"):
        store.create_or_get_by_provenance(
            **{**kwargs, "metrics": {"oos_sharpe": 1.1}}
        )
    assert store.list() == [first]


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


def test_candidate_stores_factor_mining_summary(tmp_path):
    store = CandidateStore(tmp_path)
    config = {
        "factor_name": "momentum_20d",
        "origin_run_id": "mining-run-001",
        "candidate_signature": "factor-signature",
        "regime_state": "bull",
        "algorithm_version": "mining-v1",
        "methodology_version": "factor-v2",
    }
    metrics = {
        "oos_sharpe": 1.24,
        "oos_return": 0.18,
        "oos_max_drawdown": -0.09,
        "oos_positive_fold_ratio": 0.75,
        "oos_n_trades": 48,
        "valid_folds": 4,
        "skipped_folds": 1,
        "confidence": 0.9,
        "coverage": 0.82,
        "turnover": 0.36,
        "long_short_sharpe": 1.11,
    }

    created = store.create(
        kind="factor",
        name="挖掘因子候选",
        source_id="momentum_20d",
        config=config,
        metrics=metrics,
        data_as_of="2026-08-11",
    )

    assert created["config"] == config
    assert created["metrics"] == metrics
    assert store.list()[0]["metrics"] == metrics


def test_candidate_stores_strategy_mining_factor_combination(tmp_path):
    store = CandidateStore(tmp_path)
    config = {
        "strategy_id": "mined-factor-combination",
        "origin_run_id": "mining-run-002",
        "candidate_signature": "strategy-signature",
        "regime_state": "sideways",
        "algorithm_version": "mining-v1",
        "methodology_version": "strategy-v1",
        "factor_names": ["momentum_20d", "rsi_14"],
        "directions": ["high", "low"],
        "weights": [0.6, 0.4],
    }
    metrics = {
        "oos_sharpe": 0.98,
        "oos_return": 0.12,
        "oos_max_drawdown": -0.07,
        "oos_positive_fold_ratio": 0.8,
        "oos_n_trades": 31,
        "valid_folds": 5,
        "skipped_folds": 0,
        "confidence": 0.86,
    }

    created = store.create(
        kind="strategy",
        name="挖掘组合候选",
        source_id="mined-factor-combination",
        config=config,
        metrics=metrics,
        data_as_of="2026-08-11",
    )

    assert created["config"] == config
    assert created["metrics"] == metrics
    assert store.list()[0]["config"] == config


@pytest.mark.parametrize("nested_value", [{"fold_1": 1.2}, [1.2, 0.8]])
def test_candidate_rejects_nested_mining_metrics(tmp_path, nested_value):
    store = CandidateStore(tmp_path)

    with pytest.raises(CandidateValidationError, match="只允许保存标量"):
        store.create(
            kind="factor",
            name="挖掘因子候选",
            source_id="momentum_20d",
            config={"factor_name": "momentum_20d"},
            metrics={"oos_sharpe": nested_value},
            data_as_of=None,
        )


def test_candidate_rejects_unknown_metric_field(tmp_path):
    store = CandidateStore(tmp_path)

    with pytest.raises(CandidateValidationError, match="不允许的字段"):
        store.create(
            kind="factor",
            name="挖掘因子候选",
            source_id="momentum_20d",
            config={"factor_name": "momentum_20d"},
            metrics={"fold_metrics": 1.0},
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
    path.write_text(
        json.dumps(
            [
                {
                    "id": "legacy",
                    "kind": "factor",
                    "name": "旧候选",
                    "config": {"factor_name": "rsi_14", "equity_curve": [1, 2]},
                    "metrics": {"ic_mean": 0.03, "trades": [{"symbol": "000001.SZ"}]},
                }
            ]
        ),
        encoding="utf-8",
    )

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
