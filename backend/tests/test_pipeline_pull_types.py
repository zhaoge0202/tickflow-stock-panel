"""盘后管道「A股 / ETF / 指数」拉取开关回归测试。

覆盖 issue #216: `get_pipeline_pull_a_share()` 硬编码 True 且 A股键不在
`set_pipeline_pull_types()` 白名单内, 导致取消勾选 A 股完全不生效。

纯逻辑, 不触网, 不依赖真实数据源。
"""
from __future__ import annotations

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def test_a_share_defaults_to_true_when_unset():
    """未设置时三个开关的默认值: A股/指数 默认开, ETF 默认关。"""
    assert preferences.get_pipeline_pull_a_share() is True
    assert preferences.get_pipeline_pull_index() is True
    assert preferences.get_pipeline_pull_etf() is False


def test_a_share_toggle_off_is_honored():
    """写入 False 后 getter 必须返回 False(修复前硬编码 True)。"""
    preferences.save({"pipeline_pull_a_share": False})
    assert preferences.get_pipeline_pull_a_share() is False


def test_set_pipeline_pull_types_persists_a_share():
    """setter 必须接受并落盘 A 股开关(修复前白名单遗漏该键, 被静默丢弃)。"""
    result = preferences.set_pipeline_pull_types({"pipeline_pull_a_share": False})
    assert result["pipeline_pull_a_share"] is False
    # 落盘后重新读取仍为 False
    preferences._invalidate_cache()
    assert preferences.get_pipeline_pull_a_share() is False
    assert preferences.get_pipeline_pull_types()["pipeline_pull_a_share"] is False


def test_set_pipeline_pull_types_independent_switches():
    """三个开关互相独立: 关 A 股不影响 ETF / 指数。"""
    result = preferences.set_pipeline_pull_types(
        {
            "pipeline_pull_a_share": False,
            "pipeline_pull_etf": True,
            "pipeline_pull_index": False,
        }
    )
    assert result == {
        "pipeline_pull_a_share": False,
        "pipeline_pull_etf": True,
        "pipeline_pull_index": False,
    }
