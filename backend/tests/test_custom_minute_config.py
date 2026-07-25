import math

import pytest
from pydantic import ValidationError

from app.api.settings import DatasetConfigIn
from app.data_providers.custom.config import CustomSourceConfig, _dataset_from_dict
from app.data_providers.custom.loader import _config_to_dict, _sanitize_for_yaml


def test_minute_request_parameter_names_survive_config_round_trip():
    dataset = DatasetConfigIn(
        url="https://example.test/minute",
        method="GET",
        asset_type_param="asset",
        freq_param="period",
    ).model_dump()

    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"minute": dataset},
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["minute"])
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"minute": parsed},
    ))

    assert parsed.asset_type_param == "asset"
    assert parsed.freq_param == "period"
    assert exposed["datasets"]["minute"]["asset_type_param"] == "asset"
    assert exposed["datasets"]["minute"]["freq_param"] == "period"


def test_timeout_survives_config_round_trip():
    """timeout 必须在 UI 保存往返中保留 (核心修复), 且默认 30 不污染 YAML。"""
    dataset = DatasetConfigIn(
        url="https://example.test/daily",
        method="POST",
        timeout=120.0,
    ).model_dump()

    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"daily": dataset},
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["daily"])
    exposed = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"daily": parsed},
    ))

    assert parsed.timeout == 120.0
    assert exposed["datasets"]["daily"]["timeout"] == 120.0

    # 默认 30 不 emit, 保持 YAML 干净
    default_dataset = DatasetConfigIn(url="https://example.test/realtime", method="GET").model_dump()
    cleaned2 = _sanitize_for_yaml({
        "name": "test_source",
        "display_name": "Test Source",
        "datasets": {"realtime": default_dataset},
    })
    parsed2 = _dataset_from_dict(cleaned2["datasets"]["realtime"])
    exposed2 = _config_to_dict(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"realtime": parsed2},
    ))
    assert parsed2.timeout == 30.0
    realtime = exposed2["datasets"]["realtime"]
    assert "timeout" not in realtime
    assert "symbols_param" not in realtime
    assert "start_param" not in realtime
    assert "end_param" not in realtime


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf])
def test_timeout_api_rejects_non_positive_or_non_finite_values(timeout):
    with pytest.raises(ValidationError):
        DatasetConfigIn(url="https://example.test/daily", timeout=timeout)


def test_invalid_yaml_timeout_falls_back_to_default():
    for timeout in (0, -1, math.nan, math.inf, -math.inf, "invalid"):
        parsed = _dataset_from_dict({
            "url": "https://example.test/daily",
            "timeout": timeout,
        })
        assert parsed.timeout == 30.0


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf, "invalid"])
def test_sanitizer_drops_invalid_timeout_and_realtime_request_params(timeout):
    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "realtime": {
                "url": "https://example.test/realtime",
                "timeout": timeout,
                "symbols_param": "codes",
                "start_param": "from",
                "end_param": "to",
            },
        },
    })

    dataset = cleaned["datasets"]["realtime"]
    assert "timeout" not in dataset
    assert "symbols_param" not in dataset
    assert "start_param" not in dataset
    assert "end_param" not in dataset


def test_empty_request_parameter_names_restore_defaults():
    cleaned = _sanitize_for_yaml({
        "name": "test_source",
        "datasets": {
            "minute": {
                "url": "https://example.test/minute",
                "symbols_param": " ",
                "start_param": "\t",
                "end_param": "",
            },
        },
    })
    parsed = _dataset_from_dict(cleaned["datasets"]["minute"])

    assert parsed.symbols_param == "symbols"
    assert parsed.start_param == "start_time"
    assert parsed.end_param == "end_time"
