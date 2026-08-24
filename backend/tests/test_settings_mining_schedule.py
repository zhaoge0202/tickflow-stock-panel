from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api import settings


def test_mining_schedule_model_is_strict_and_forbids_extra_fields():
    valid = settings.MiningSchedulePrefs(
        mining_schedule_enabled=True,
        mining_schedule_weekday=4,
        mining_budget_profile="strict",
    )
    assert valid.model_dump() == {
        "mining_schedule_enabled": True,
        "mining_schedule_weekday": 4,
        "mining_budget_profile": "strict",
    }

    invalid_payloads = [
        {
            "mining_schedule_enabled": "true",
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
        {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 5,
            "mining_budget_profile": "balanced",
        },
        {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "exploratory",
        },
        {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
            "unknown": True,
        },
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            settings.MiningSchedulePrefs.model_validate(payload)


def test_update_mining_schedule_calls_group_setter_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.preferences.set_mining_schedule",
        lambda enabled, weekday, profile: (
            calls.append((enabled, weekday, profile))
            or {
                "mining_schedule_enabled": enabled,
                "mining_schedule_weekday": weekday,
                "mining_budget_profile": profile,
            }
        ),
    )
    request = settings.MiningSchedulePrefs(
        mining_schedule_enabled=True,
        mining_schedule_weekday=1,
        mining_budget_profile="strict",
    )

    result = settings.update_mining_schedule(request)

    assert calls == [(True, 1, "strict")]
    assert result["mining_budget_profile"] == "strict"
