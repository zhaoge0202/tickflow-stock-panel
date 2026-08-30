"""全量分钟能力 (Cap.INTRADAY_UNIVERSE) 契约。

- 能力位存在且值为 "intraday.universe"
- tiers.yaml 仅 expert 档声明该能力 (Pro/自定义源天然没有)
- 探测层注册了该能力的探测调用, 显示标签为「全量分钟」
- 缓存 schema 已 bump (旧 capabilities.json 触发重探测)
- 盘中分钟服务门控挂在该能力位上
"""
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.services.minute_refresh import MinuteRefreshService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.policy import _CACHE_SCHEMA_VERSION, _CAP_ALIASES, _load_tiers_yaml


def test_capability_enum_value():
    assert Cap("intraday.universe") is Cap.INTRADAY_UNIVERSE


def test_tiers_yaml_grants_universe_to_expert_only():
    tiers = _load_tiers_yaml()
    assert "intraday.universe" in tiers["expert"]
    for tier in ("free", "starter", "pro"):
        assert "intraday.universe" not in tiers[tier]


def test_policy_labels_and_cache_schema():
    assert _CAP_ALIASES[Cap.INTRADAY_UNIVERSE] == "全量分钟"
    assert _CACHE_SCHEMA_VERSION >= 6


def test_service_gate_requires_universe_not_batch_alone():
    class _Repo:
        store = SimpleNamespace(data_dir=Path("."))

        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame({"symbol": []})

    def _svc_with(caps: dict) -> MinuteRefreshService:
        svc = MinuteRefreshService(_Repo())
        svc.set_app_state(SimpleNamespace(capabilities=CapabilitySet(caps)))
        return svc

    universe = {Cap.INTRADAY_UNIVERSE: CapabilityLimits(rpm=20)}
    batch_only = {Cap.INTRADAY_BATCH: CapabilityLimits(rpm=60, batch=200)}

    assert _svc_with(universe).capability_ok() is True
    assert _svc_with(batch_only).capability_ok() is False       # 仅有 intraday.batch 不放行
    assert _svc_with({}).capability_ok() is False
