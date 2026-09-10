"""AI 轮动分析提示词里的「大盘背景」指数涨跌幅必须按百分数口径。

CONTRIBUTING §3.1: build_market_overview 的 indices[].change_pct 是百分数
(quote_service._build_index_quotes 在数据边界已乘过 100, DB 兜底路径同样乘过 100)。
概念/行业涨幅则是小数。两者共用一个 _fmt_pct 会把指数再放大 100 倍,
喂给 LLM 的大盘背景变成「上证指数 +123.00%」。
"""
from __future__ import annotations

from app.services.concept_rotation_analyzer import (
    _build_market_block,
    _build_signal_block,
)


def _overview() -> dict:
    """与 build_market_overview 返回结构一致的最小切片。"""
    return {
        "indices": [
            {"symbol": "000001.SH", "name": "上证指数", "change_pct": 1.23},
            {"symbol": "399001.SZ", "name": "深证成指", "change_pct": -0.45},
        ],
        "emotion": {"score": 62, "label": "偏暖"},
        "limit": {"limit_up": 68, "broken": 20, "limit_down": 3, "max_boards": 5},
        "amount": {"total": 1.2e12},
    }


def test_index_change_pct_is_not_scaled_again():
    block = _build_market_block(_overview())

    assert "上证指数 +1.23%" in block
    assert "深证成指 -0.45%" in block
    assert "123.00%" not in block


def test_missing_index_change_pct_renders_placeholder():
    overview = _overview()
    overview["indices"][0]["change_pct"] = None

    assert "上证指数 —" in _build_market_block(overview)


def test_dimension_pct_is_still_scaled_from_decimal():
    """概念/行业涨幅仍是小数口径, 必须乘 100 后展示 (不能被一起改掉)。"""
    items = [{
        "concept": "人工智能",
        "ranks": [3, 1],
        "pcts": [0.05, 0.07],
        "avg_rank": 2.0,
        "rank_std": 1.0,
    }]

    block = _build_signal_block("主线", items)

    assert "区间均涨 +6.00%" in block
