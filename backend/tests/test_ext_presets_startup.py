"""内置概念/行业 preset 启动不得自动拉取 (#199)。

ensure_builtin_presets 的契约是「只创建 config.json, 不拉取数据, 等待用户手动获取」;
但 preset 出厂 PullConfig.enabled=True 会让 PullScheduler.refresh 在启动时立即调度
_run_loop 并马上执行一次网络拉取 (启用后立即执行一次), 与契约矛盾。

回归断言: 内置 preset 出厂 pull.enabled 必须为 False —— scheduler 的 enabled 过滤
(ext_pull.refresh) 会因此跳过它们; 手动获取走 fetch_preset 独立路径, 不经过本开关
(该路径行为由 test_ext_preset_dimension_values / test_ext_pull_refresh 覆盖)。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.ext_data import ExtConfigStore
from app.services.ext_presets import (
    _concept_preset,
    _industry_preset,
    ensure_builtin_presets,
)

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


def test_builtin_presets_ship_disabled() -> None:
    """出厂 preset 的 pull.enabled 必须为 False, 否则启动即网络拉取 (#199)。"""
    for preset in (_concept_preset(), _industry_preset()):
        assert preset.pull is not None
        assert preset.pull.url, "禁用归禁用, 手动获取仍需 url 配方"
        assert preset.pull.enabled is False, f"{preset.id} 启动即自动拉取, 违反启动契约"


def test_ensure_builtin_presets_writes_disabled_configs(tmp_path: Path) -> None:
    """全新数据目录: 启动只落禁用的 pull 配置, scheduler 扫描后无任务可建。"""
    asyncio.run(ensure_builtin_presets(tmp_path))

    store = ExtConfigStore(tmp_path)
    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None, f"{cid} 配置未创建"
        assert config.pull is not None
        assert config.pull.enabled is False


def test_ensure_builtin_presets_keeps_existing_user_config(tmp_path: Path) -> None:
    """老用户/已存在的配置一律不动: 即使保留 enabled=True 也不被覆盖。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    config = store.get("ext_gn_ths")
    assert config is not None and config.pull is not None
    config.pull.enabled = True
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None and refreshed.pull is not None
    assert refreshed.pull.enabled is True, "已存在配置被静默改写, 违反「绝不覆盖」原则"
