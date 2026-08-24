from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import backtest as api
from app.backtest.factor import FACTOR_COLUMNS


def test_factor_batch_api_rejects_unknown_factor():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    req = api.FactorBatchRequest(factor_names=["unknown"])

    with pytest.raises(HTTPException) as exc_info:
        api.factor_batch(req, request)
    assert exc_info.value.status_code == 400
    assert "unknown" in str(exc_info.value.detail)


def test_factor_batch_request_accepts_full_research_catalog():
    factor_names = [item["id"] for item in FACTOR_COLUMNS]

    request = api.FactorBatchRequest(factor_names=factor_names)

    assert len(request.factor_names) > 16
    assert request.factor_names == factor_names


def test_candidate_api_create_list_and_update(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "data_dir", tmp_path)
    created = api.candidate_create(api.CandidateCreateRequest(
        kind="factor",
        name="RSI 候选",
        source_id="rsi_14",
        config={"factor_name": "rsi_14"},
        metrics={"ic_mean": 0.03},
        data_as_of=date(2026, 8, 11),
    ))

    assert api.candidates_list()["items"][0]["id"] == created["id"]
    updated = api.candidate_update(
        created["id"],
        api.CandidateUpdateRequest(status="validated"),
    )
    assert updated["status"] == "validated"


def test_candidate_api_returns_clear_error_for_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "research_candidates.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        api.candidates_list()
    assert exc_info.value.status_code == 500
    assert "损坏" in str(exc_info.value.detail)
