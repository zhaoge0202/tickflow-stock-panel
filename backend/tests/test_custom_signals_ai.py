"""自定义信号 AI 生成 — prompt 构建与解析校验测试。

覆盖:
  - build_messages: prompt 包含字段白名单、运算符、MAX_DAYS
  - parse_and_validate: 合法 JSON / 白名单外字段 / 非法 JSON / markdown 围栏
  - API 端点: 成功 / 校验失败 400 / 空描述 400 / AI 运行时错误透出
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi import HTTPException

from app.api.signals import AIGenerateRequest, ai_generate_signal
from app.strategy.custom_signals_ai import build_messages, parse_and_validate

VALID_JSON = '''{
  "name": "回踩MA20放量",
  "conditions": [
    {"left": "close", "op": "<=", "right": "field:ma20", "leftDays": 0, "rightDays": 0},
    {"left": "vol_ratio_5d", "op": ">=", "right": 2, "leftDays": 0, "rightDays": 0}
  ]
}'''


# ── build_messages ──────────────────────────────────────────


def test_build_messages_contains_whitelist_fields_and_rules():
    messages = build_messages("回踩MA20且放量")
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "close" in system
    assert "vol_ratio_5d" in system
    assert ">=" in system
    assert "MAX_DAYS" in system or "60" in system
    assert "回踩MA20且放量" in user
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_build_messages_only_contains_whitelisted_fields():
    from app.strategy.custom_signals import ALLOWED_FIELDS

    system = build_messages("x")[0]["content"]
    # 只检查「字段清单」段落（可用字段 … 运算符），排除 JSON 格式示例里的 "name"
    field_section = system.split("运算符（op）", 1)[0]
    for field in ("ma20", "rsi_14", "boll_upper"):
        assert field in field_section
    # 字段清单里出现的每个 key( 都必须在白名单内
    keys = set(re.findall(r"([a-z0-9_]+)\(", field_section))
    assert keys and keys <= ALLOWED_FIELDS


# ── parse_and_validate ──────────────────────────────────────


def test_parse_and_validate_valid():
    result = parse_and_validate(VALID_JSON)
    assert result["name"] == "回踩MA20放量"
    conds = result["conditions"]
    assert len(conds) == 2
    # 数字右值归一化为字符串
    assert conds[1]["right"] == "2"
    assert conds[0]["right"] == "field:ma20"
    # 缺省偏移补 0
    assert conds[0]["leftDays"] == 0
    assert conds[0]["rightDays"] == 0


def test_parse_and_validate_handles_missing_days():
    raw = json.dumps({
        "name": "新低反转",
        "conditions": [
            {"left": "close", "op": "<=", "right": "field:low_60d"}
        ],
    })
    result = parse_and_validate(raw)
    assert result["conditions"][0]["leftDays"] == 0
    assert result["conditions"][0]["rightDays"] == 0


def test_parse_and_validate_handles_markdown_fence():
    wrapped = f"```json\n{VALID_JSON}\n```"
    result = parse_and_validate(wrapped)
    assert result["name"] == "回踩MA20放量"


def test_parse_and_validate_rejects_non_whitelist_field():
    raw = json.dumps({
        "name": "非法字段",
        "conditions": [{"left": "not_a_field", "op": ">", "right": "1"}],
    })
    with pytest.raises(ValueError, match="not_a_field"):
        parse_and_validate(raw)


def test_parse_and_validate_rejects_bad_operator():
    raw = json.dumps({
        "name": "非法运算符",
        "conditions": [{"left": "close", "op": "=~", "right": "1"}],
    })
    with pytest.raises(ValueError):
        parse_and_validate(raw)


def test_parse_and_validate_rejects_invalid_json():
    with pytest.raises(ValueError, match="JSON"):
        parse_and_validate("这不是 JSON")


def test_parse_and_validate_rejects_empty_conditions():
    raw = json.dumps({"name": "空条件", "conditions": []})
    with pytest.raises(ValueError):
        parse_and_validate(raw)


def test_parse_and_validate_accepts_bare_whitelist_field_rhs():
    # AI 漏写 field: 前缀: 右值裸写白名单字段, 应自动补全为 field: 形式
    raw = json.dumps({
        "name": "MACD金叉",
        "conditions": [
            {"left": "macd_dif", "op": ">", "right": "macd_dea"}
        ],
    })
    result = parse_and_validate(raw)
    assert result["conditions"][0]["right"] == "field:macd_dea"


def test_parse_and_validate_rejects_bare_non_whitelist_field_rhs():
    # 裸写非白名单字段作为右值, 仍应报非法右值
    raw = json.dumps({
        "name": "非法右值",
        "conditions": [
            {"left": "close", "op": ">", "right": "not_a_field"}
        ],
    })
    with pytest.raises(ValueError, match="非法右值"):
        parse_and_validate(raw)


def test_validate_accepts_bare_field_rhs():
    # 解析器层面: 裸字段右值在白名单内即视为字段引用, 不抛异常
    from app.strategy import custom_signals

    sig = {
        "id": "test_bare_rhs",
        "name": "测试",
        "kind": "entry",
        "conditions": [
            {"left": "macd_dif", "op": ">", "right": "macd_dea",
             "leftDays": 0, "rightDays": 0},
        ],
    }
    custom_signals.validate(sig)


def test_parse_and_validate_accepts_json_with_trailing_comma():
    # AI 在数组结尾多加逗号 (,]): 应被容错
    raw = '{"name": "测试", "conditions": [{"left": "close", "op": ">", "right": "1", "leftDays": 0, "rightDays": 0},]}'
    result = parse_and_validate(raw)
    assert result["name"] == "测试"
    assert len(result["conditions"]) == 1


def test_parse_and_validate_accepts_json_with_prose_wrap():
    # AI 混入前后解释文字: 应提取首个 {...} 平衡块
    raw = f"好的, 我设计了如下信号:\n{VALID_JSON}\n希望对你有所帮助。"
    result = parse_and_validate(raw)
    assert result["name"] == "回踩MA20放量"


def test_parse_and_validate_accepts_json_with_trailing_garbage():
    # 无围栏 + 尾随垃圾字符: 截到最后一个 } 后仍应解析成功
    raw = '{"name": "测试", "conditions": [{"left": "close", "op": ">", "right": "1", "leftDays": 0, "rightDays": 0}]} 这是额外说明'
    result = parse_and_validate(raw)
    assert result["name"] == "测试"


# ── API 端点 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_generate_endpoint_success(monkeypatch):
    captured: dict = {}

    async def fake_generate(messages, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        return VALID_JSON

    import app.services.ai_provider as ai_provider

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    result = await ai_generate_signal(AIGenerateRequest(description="回踩MA20且放量"))
    assert result["name"] == "回踩MA20放量"
    assert len(result["conditions"]) == 2
    # 复杂描述 (多条件) 不设输出上限: 推理模型思考 token 计入 max_tokens 预算,
    # 显式限制会挤占正文导致 JSON 截断 (对齐分析器的放开策略)
    assert captured["max_tokens"] is None


@pytest.mark.asyncio
async def test_ai_generate_endpoint_400_on_invalid_conditions(monkeypatch):
    async def fake_generate(messages, **kwargs):
        return json.dumps({
            "name": "非法",
            "conditions": [{"left": "close", "op": ">", "right": "field:not_allowed"}],
        })

    import app.services.ai_provider as ai_provider

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    with pytest.raises(HTTPException) as exc_info:
        await ai_generate_signal(AIGenerateRequest(description="测试"))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_ai_generate_endpoint_400_on_empty_description():
    with pytest.raises(HTTPException) as exc_info:
        await ai_generate_signal(AIGenerateRequest(description="   "))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_ai_generate_endpoint_passes_runtime_error(monkeypatch):
    async def fake_generate(messages, **kwargs):
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")

    import app.services.ai_provider as ai_provider

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    with pytest.raises(HTTPException) as exc_info:
        await ai_generate_signal(AIGenerateRequest(description="测试"))
    assert exc_info.value.status_code == 400
    assert "AI API Key" in exc_info.value.detail
