from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.strategy import (
    StrategyCodeSaveRequest,
    StrategyCodeValidateRequest,
    _prepare_strategy_code,
    _save_strategy_code,
)
from app.strategy.engine import StrategyEngine


def _code(strategy_id: str, name: str = "测试策略") -> str:
    return f'''"""测试策略"""
import polars as pl

META = {{
    "id": "{strategy_id}",
    "name": "{name}",
    "description": "测试描述",
    "tags": ["测试"],
    "params": [],
    "scoring": {{}},
}}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def _request(tmp_path):
    ai_dir = tmp_path / "strategies" / "ai"
    custom_dir = tmp_path / "strategies" / "custom"
    engine = StrategyEngine(strategy_dirs=[custom_dir, ai_dir])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))


def test_prepare_strategy_code_rejects_forbidden_import():
    req = StrategyCodeValidateRequest(
        strategy_id="custom_bad",
        code='''import os\nMETA = {"id": "custom_bad"}\n''',
    )

    with pytest.raises(ValueError, match="禁止 import os"):
        _prepare_strategy_code(req)


def test_prepare_strategy_code_rejects_unknown_scoring_field():
    req = StrategyCodeValidateRequest(
        strategy_id="custom_bad_score",
        code=_code("custom_bad_score").replace(
            '"scoring": {},',
            '"scoring": {"volume_surge": 1.0},',
        ),
    )

    with pytest.raises(ValueError, match="volume_surge"):
        _prepare_strategy_code(req)


def test_save_strategy_code_creates_ai_strategy_in_ai_dir(tmp_path):
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id="ai_saved",
        target_source="ai",
        mode="create",
        code=_code("wrong"),
        name="AI 策略",
    )

    result = _save_strategy_code(req, request)

    assert result["ok"] is True
    assert result["source"] == "ai"
    assert (tmp_path / "strategies" / "ai" / "ai_saved.py").exists()
    loaded = request.app.state.strategy_engine.get("ai_saved")
    assert loaded.source == "ai"
    assert loaded.file_path == tmp_path / "strategies" / "ai" / "ai_saved.py"


def test_save_strategy_code_creates_custom_strategy_in_custom_dir(tmp_path):
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id="custom_saved",
        target_source="custom",
        mode="create",
        code=_code("wrong"),
        name="自定义策略",
    )

    result = _save_strategy_code(req, request)

    assert result["ok"] is True
    assert result["source"] == "custom"
    assert (tmp_path / "strategies" / "custom" / "custom_saved.py").exists()
    loaded = request.app.state.strategy_engine.get("custom_saved")
    assert loaded.source == "custom"
    assert loaded.file_path == tmp_path / "strategies" / "custom" / "custom_saved.py"


def test_save_strategy_code_updates_existing_source_file(tmp_path):
    request = _request(tmp_path)
    create = StrategyCodeSaveRequest(
        strategy_id="custom_update",
        target_source="custom",
        mode="create",
        code=_code("custom_update", "旧名称"),
    )
    _save_strategy_code(create, request)

    update = StrategyCodeSaveRequest(
        strategy_id="custom_update",
        target_source="ai",
        mode="update",
        code=_code("custom_update", "新名称"),
    )
    result = _save_strategy_code(update, request)

    assert result["source"] == "custom"
    custom_path = tmp_path / "strategies" / "custom" / "custom_update.py"
    assert custom_path.exists()
    assert not (tmp_path / "strategies" / "ai" / "custom_update.py").exists()
    assert '"name": "新名称"' in custom_path.read_text(encoding="utf-8")


def test_save_strategy_code_rejects_undefined_custom_signal(tmp_path):
    """REQUIRED_FEATURES 引用未定义的自定义信号 → 拒绝保存并恢复文件。

    回归: 之前保存不校验, 运行期才抛 polars 缺列错 (500)。
    """
    request = _request(tmp_path)
    code = _code("custom_missing_sig") + (
        '\nREQUIRED_FEATURES = {"csg_oversold_macd_about_to_golden"}\n'
    )
    req = StrategyCodeSaveRequest(
        strategy_id="custom_missing_sig",
        target_source="custom",
        mode="create",
        code=code,
        name="引用不存在信号的策略",
    )

    with pytest.raises(ValueError, match="csg_oversold_macd_about_to_golden"):
        _save_strategy_code(req, request)

    # 校验失败不落盘
    assert not (tmp_path / "strategies" / "custom" / "custom_missing_sig.py").exists()


def test_save_strategy_code_ok_when_custom_signal_defined(tmp_path):
    """信号已定义时, 引用它的策略可以正常保存。"""
    from app.strategy import custom_signals

    custom_signals.save_one(tmp_path, {
        "id": "oversold_macd_about_to_golden",
        "name": "超跌接近金叉",
        "kind": "entry",
        "conditions": [
            {"left": "momentum_60d", "op": "<=", "right": "-0.30",
             "leftDays": 0, "rightDays": 0},
        ],
        "enabled": True,
    })
    request = _request(tmp_path)
    code = _code("custom_with_sig") + (
        '\nREQUIRED_FEATURES = {"csg_oversold_macd_about_to_golden"}\n'
    )
    req = StrategyCodeSaveRequest(
        strategy_id="custom_with_sig",
        target_source="custom",
        mode="create",
        code=code,
        name="引用已定义信号的策略",
    )

    result = _save_strategy_code(req, request)
    assert result["ok"] is True
    loaded = request.app.state.strategy_engine.get("custom_with_sig")
    assert "csg_oversold_macd_about_to_golden" in loaded.required_features
