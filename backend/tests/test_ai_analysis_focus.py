"""AI 分析关注重点的统一 Prompt 契约测试。"""

from app.services.ai_provider import build_focus_instruction, sanitize_focus
from app.services.concept_rotation_analyzer import (
    _build_user_prompt as build_rotation_user_prompt,
)
from app.services.financial_analyzer import (
    _build_user_prompt as build_financial_user_prompt,
)
from app.services.market_recap import (
    _build_user_prompt as build_recap_user_prompt,
)
from app.services.stock_analyzer import (
    _build_user_prompt as build_stock_user_prompt,
)


def test_focus_whitespace_is_normalized() -> None:
    assert sanitize_focus("  支撑位\n  多少  ") == "支撑位 多少"


def test_trade_wording_is_reframed_instead_of_silently_dropped() -> None:
    instruction = build_focus_instruction("现在能买吗,目标价多少", report_name="个股分析报告")

    assert "用户关注: 现在能买吗,目标价多少" in instruction
    assert "关注重点回应" in instruction
    assert "不得给出相应操作结论" in instruction
    assert "关键价位" in instruction


def test_safe_focus_requires_a_direct_answer_without_extra_warning() -> None:
    instruction = build_focus_instruction("支撑位多少", report_name="个股分析报告")

    assert "用户关注: 支撑位多少" in instruction
    assert "用 2-4 条带具体数据的结论直接回应" in instruction
    assert "不得给出相应操作结论" not in instruction


def test_all_focus_enabled_analyzers_share_the_priority_contract() -> None:
    focus = "支撑位多少"
    prompts = [
        build_stock_user_prompt([], {}, {}, 10.0, "600000.SH", focus),
        build_financial_user_prompt({}, "600000.SH", focus),
        build_recap_user_prompt({}, [], focus),
        build_rotation_user_prompt({}, {}, 12, [], focus),
    ]

    for prompt in prompts:
        assert "## 用户关注重点(必须优先回应)" in prompt
        assert f"用户关注: {focus}" in prompt
        assert "### 0. 🔎 关注重点回应" in prompt


def test_empty_focus_does_not_add_focus_section() -> None:
    prompts = [
        build_stock_user_prompt([], {}, {}, 10.0, "600000.SH", ""),
        build_financial_user_prompt({}, "600000.SH", ""),
        build_recap_user_prompt({}, [], ""),
        build_rotation_user_prompt({}, {}, 12, [], ""),
    ]

    assert all("用户关注重点" not in prompt for prompt in prompts)
