"""回测参数归一化: select 的候选值必须按 {label,value} 的 value 判定。

回归背景: options 的规范形态是 [{"label": .., "value": ..}], 旧实现直接
拿 value 去 `in param["options"]`, 永远为假 -> 字符串型 select 参数被静默
回落成 default, 用户在回测里切了选项却跑的还是默认值, 且没有任何提示。
"""
from __future__ import annotations

from app.backtest.strategy import StrategyBacktestService
from app.strategy.engine import StrategyDef

normalize = StrategyBacktestService._normalize_params


def _strategy(params: list[dict]) -> StrategyDef:
    """只构造 _normalize_params 需要的 meta; 其余字段用占位值。"""
    return StrategyDef(
        meta={"params": params},
        basic_filter={},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=1,
        source="test",
    )


def test_dict_options_keep_selected_value():
    s = _strategy([
        {
            "id": "entry_mode",
            "type": "select",
            "default": "burst",
            "options": [
                {"label": "拉直线当根", "value": "burst"},
                {"label": "回踩均价线", "value": "retest"},
            ],
        }
    ])
    assert normalize({"entry_mode": "retest"}, s)["entry_mode"] == "retest"
    assert normalize({}, s)["entry_mode"] == "burst"


def test_int_valued_dict_options_keep_selected_value():
    s = _strategy([
        {
            "id": "burst_bars",
            "type": "select",
            "default": 1,
            "options": [{"label": "1根", "value": 1}, {"label": "2根", "value": 2}],
        }
    ])
    assert normalize({"burst_bars": 2}, s)["burst_bars"] == 2


def test_invalid_select_value_falls_back_to_default():
    s = _strategy([
        {
            "id": "entry_mode",
            "type": "select",
            "default": "burst",
            "options": [{"label": "a", "value": "burst"}, {"label": "b", "value": "retest"}],
        }
    ])
    assert normalize({"entry_mode": "nope"}, s)["entry_mode"] == "burst"


def test_plain_string_options_still_supported():
    s = _strategy([
        {"id": "mode", "type": "select", "default": "a", "options": ["a", "b"]}
    ])
    assert normalize({"mode": "b"}, s)["mode"] == "b"
