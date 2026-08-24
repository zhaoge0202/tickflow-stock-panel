from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

import pytest

from app.services.mining_jobs import (
    ACTIVE_RUN_STATUSES,
    MAX_EVENT_PAYLOAD_BYTES,
    SUCCESS_RUN_STATUSES,
    InvalidMiningStatusTransitionError,
    MiningRunStore,
    MiningRunValidationError,
    canonicalize_request,
    compute_run_signature,
)


def _store(tmp_path: Path) -> MiningRunStore:
    return MiningRunStore(tmp_path)


def test_create_and_atomic_summary_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = store.create(
        {"symbols": ["000001.SZ"], "start": date(2025, 1, 1)},
        {"daily_generation": 7},
        run_id="run_atomic",
    )
    run_dir = tmp_path / "research" / "mining" / "runs" / "run_atomic"

    assert manifest["status"] == "queued"
    assert manifest["request"]["start"] == "2025-01-01"
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert store.read_summary("run_atomic") == {}

    failures: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                value = store.read_summary("run_atomic")
                assert isinstance(value.get("iteration"), int)
            except Exception as exc:
                failures.append(exc)
                return

    store.write_summary("run_atomic", {"iteration": -1})
    thread = threading.Thread(target=reader)
    thread.start()
    for iteration in range(50):
        store.write_summary("run_atomic", {"iteration": iteration})
    stop.set()
    thread.join(timeout=1)

    assert failures == []
    assert store.read_summary("run_atomic") == {"iteration": 49}
    assert not list(run_dir.glob("*.tmp"))
    assert not list(run_dir.glob(".*.tmp"))


def test_signature_is_canonical_and_covers_request_dimensions() -> None:
    first = {
        "symbols": ["000001.SZ", "600000.SH"],
        "window": {"start": "2025-01-01", "end": "2025-06-30"},
        "budget": 100,
    }
    reordered = {
        "budget": 100,
        "window": {"end": "2025-06-30", "start": "2025-01-01"},
        "symbols": ["000001.SZ", "600000.SH"],
    }

    assert canonicalize_request(first) == canonicalize_request(reordered)
    signature = compute_run_signature(first, {"daily": "v7", "enriched": "v2"})
    assert signature == compute_run_signature(reordered, {"enriched": "v2", "daily": "v7"})
    assert len(signature) == 64
    assert signature != compute_run_signature(
        {**first, "budget": 101}, {"daily": "v7", "enriched": "v2"}
    )
    assert signature != compute_run_signature(first, {"daily": "v8", "enriched": "v2"})
    assert signature != compute_run_signature(
        {**first, "symbols": list(reversed(first["symbols"]))},
        {"daily": "v7", "enriched": "v2"},
    )


def test_events_are_bounded_monotonic_and_support_after_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create({}, "data-v1", run_id="event_run")

    for index in range(300):
        event = store.append_event("event_run", "progress", {"step": index})
        assert event["id"] == index + 1

    retained = store.read_events("event_run")
    assert len(retained) == 256
    assert [event["id"] for event in retained] == list(range(45, 301))
    assert [event["id"] for event in store.read_events("event_run", after_id=295)] == list(
        range(296, 301)
    )

    with pytest.raises(MiningRunValidationError, match="payload exceeds"):
        store.append_event(
            "event_run",
            "result",
            {"large_result": "x" * (MAX_EVENT_PAYLOAD_BYTES + 1)},
        )


def test_status_transitions_summary_and_artifact_registration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create({"budget": 10}, "data-v1", run_id="lifecycle")

    running = store.transition_status("lifecycle", "running")
    assert running["started_at"] is not None
    cancelling = store.transition_status("lifecycle", "cancelling")
    assert cancelling["cancellation_requested_at"] is not None
    cancelled = store.transition_status("lifecycle", "cancelled")
    assert cancelled["finished_at"] is not None

    with pytest.raises(InvalidMiningStatusTransitionError):
        store.transition_status("lifecycle", "running")

    store.create({}, "data-v1", run_id="artifacts")
    artifact = store.artifact_path("artifacts", "factors")
    manifest = store.register_artifact("artifacts", "factors", artifact)
    assert artifact.name == "factors.parquet"
    assert manifest["artifacts"] == {"factors": "factors.parquet"}
    assert store.write_summary("artifacts", {"candidate_count": 3}) == {"candidate_count": 3}

    with pytest.raises(MiningRunValidationError, match="escapes"):
        store.register_artifact("artifacts", "folds", "../other/folds.parquet")


def test_historical_manifest_defaults_and_startup_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runs_root = store.runs_root
    old_dir = runs_root / "old_running"
    old_dir.mkdir()
    (old_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "old_running",
                "status": "running",
                "request": {"budget": 10},
                "data_fingerprint": "v1",
                "created_at": "2025-01-01T00:00:00+00:00",
                "artifacts": {
                    "factors": "factors.parquet",
                    "folds": "../escaped/folds.parquet",
                    "unknown": "unknown.parquet",
                },
            }
        ),
        encoding="utf-8",
    )
    store.create({}, "v1", run_id="was_cancelling")
    store.transition_status("was_cancelling", "cancelling")
    store.create({}, "v1", run_id="still_queued")

    historical = store.get("old_running")
    assert historical is not None
    assert historical["artifacts"] == {"factors": "factors.parquet"}
    assert historical["finished_at"] is None
    assert historical["run_signature"] == compute_run_signature({"budget": 10}, "v1")

    assert store.recover_interrupted() == 3
    assert store.get("old_running")["status"] == "interrupted"  # type: ignore[index]
    assert store.get("was_cancelling")["status"] == "interrupted"  # type: ignore[index]
    assert store.get("still_queued")["status"] == "interrupted"  # type: ignore[index]
    assert store.recover_interrupted() == 0


def test_run_ids_and_paths_are_restricted_to_runs_root(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for run_id in ["", ".", "..", "../escape", "a/b", "a\\b", "with space"]:
        with pytest.raises(MiningRunValidationError):
            store.get(run_id)

    with pytest.raises(MiningRunValidationError):
        store.create({}, "v1", run_id="../escape")
    with pytest.raises(MiningRunValidationError):
        store.create({}, "v1", run_id="")
    assert not (tmp_path / "research" / "mining" / "escape").exists()


def test_find_by_signature_can_filter_active_and_success_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    queued = store.create({"budget": 1}, "v1", run_id="queued_match")
    store.create({"budget": 1}, "v1", run_id="failed_match")
    store.transition_status("failed_match", "failed", error="failed")
    store.create({"budget": 2}, "v1", run_id="success_match")
    store.transition_status("success_match", "running")
    store.transition_status("success_match", "succeeded_with_budget_exhausted")

    assert (
        store.find_by_signature(queued["run_signature"], statuses=ACTIVE_RUN_STATUSES)["run_id"]
        == "queued_match"
    )  # type: ignore[index]
    assert store.find_by_signature(queued["run_signature"], statuses=SUCCESS_RUN_STATUSES) is None
    success = store.get("success_match")
    assert success is not None
    assert (
        store.find_by_signature(success["run_signature"], statuses=SUCCESS_RUN_STATUSES)["run_id"]
        == "success_match"
    )  # type: ignore[index]


def test_list_runs_is_bounded_sorted_and_filters_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create({"budget": 1}, "v1", run_id="first")
    store.create({"budget": 2}, "v1", run_id="second")
    store.transition_status("second", "running")
    store.transition_status("second", "succeeded")

    runs = store.list_runs(limit=1)
    assert [run["run_id"] for run in runs] == ["second"]
    assert [run["run_id"] for run in store.list_runs(statuses={"queued"})] == ["first"]

    with pytest.raises(MiningRunValidationError, match="limit"):
        store.list_runs(limit=0)
    with pytest.raises(MiningRunValidationError, match="statuses"):
        store.list_runs(statuses={"unknown"})  # type: ignore[arg-type]


def test_concurrent_event_appends_have_unique_contiguous_ids(tmp_path: Path) -> None:
    first_store = _store(tmp_path)
    second_store = _store(tmp_path)
    first_store.create({}, "v1", run_id="concurrent")
    barrier = threading.Barrier(5)
    failures: list[Exception] = []

    def append_batch(store: MiningRunStore, worker: int) -> None:
        try:
            barrier.wait()
            for index in range(30):
                store.append_event("concurrent", "progress", {"worker": worker, "index": index})
        except Exception as exc:
            failures.append(exc)

    threads = [
        threading.Thread(
            target=append_batch,
            args=(first_store if worker % 2 == 0 else second_store, worker),
        )
        for worker in range(4)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    ids = [event["id"] for event in first_store.read_events("concurrent")]
    assert ids == list(range(1, 121))
