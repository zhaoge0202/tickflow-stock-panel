"""required_history_bars 对 filter_history 策略的历史窗口判定。

回归: 自定义策略只把 lookback_days 声明为参数(未声明模块级 LOOKBACK_DAYS)时,
strategy.lookback_days 回退到 1, required_history_bars 返回 1,
build_strategy_context 因此跳过加载历史 (history_bars > 1 不成立),
engine.run 对 filter_history 策略报 "requires history data"。
"""
import dataclasses
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.screener import ScreenerService
from app.strategy.engine import StrategyDef, StrategyEngine


def _filter_history_strategy(
    sid: str,
    *,
    lookback_days: int = 1,
    param_default: int | None = 22,
) -> StrategyDef:
    params = [
        {"id": "lookback_days", "label": "回看窗口（天）", "type": "int", "default": param_default},
        {"id": "max_drawdown_thresh", "type": "float", "default": -0.25},
    ]
    return StrategyDef(
        meta={"id": sid, "params": params, "scoring": {}, "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        filter_fn=None,
        filter_history_fn=lambda df, params: df,
        lookback_days=lookback_days,
        source="custom",
    )


def _make_engine(*strategies: StrategyDef) -> StrategyEngine:
    engine = StrategyEngine(strategy_dirs=[])
    for s in strategies:
        engine._strategies[s.meta["id"]] = s
    return engine


def test_required_history_bars_uses_resolved_lookback_days_param():
    """策略只声明 lookback_days 参数(默认 22)时, 应加载 22 天历史而非静态回退的 1 天。"""
    engine = _make_engine(
        _filter_history_strategy("custom_window", lookback_days=1, param_default=22)
    )
    assert engine.required_history_bars(["custom_window"]) == 22


def test_required_history_bars_respects_saved_param_override():
    """用户保存的 lookback_days 参数应覆盖默认值。"""
    engine = _make_engine(
        _filter_history_strategy("custom_saved", lookback_days=1, param_default=22)
    )
    bars = engine.required_history_bars(
        ["custom_saved"],
        params_map={"custom_saved": {"lookback_days": 30}},
        overrides_map={"custom_saved": {}},
    )
    assert bars == 30


def test_required_history_bars_falls_back_to_static_lookback_days():
    """无 lookback_days 参数、声明 LOOKBACK_DAYS=8 的策略, 沿用静态值 8。"""
    engine = _make_engine(
        _filter_history_strategy("custom_static", lookback_days=8, param_default=None)
    )
    assert engine.required_history_bars(["custom_static"]) == 8


def test_required_history_bars_takes_max_of_static_and_param():
    """静态 LOOKBACK_DAYS 与参数窗口并存时取较大者, 保证窗口数据充足。"""
    engine = _make_engine(
        _filter_history_strategy("custom_both", lookback_days=8, param_default=22)
    )
    assert engine.required_history_bars(["custom_both"]) == 22


def test_build_strategy_context_loads_history_for_param_lookback(tmp_path, monkeypatch):
    """service 层回归: filter_history 策略只声明 lookback_days 参数时,
    build_strategy_context 应按参数加载历史, 使 engine.run 不再报 requires history data。"""
    engine = _make_engine(
        _filter_history_strategy("custom_window", lookback_days=1, param_default=22)
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    svc = ScreenerService(repo)

    captured: dict[str, int] = {}

    def fake_load(target_date: date, lookback_days: int) -> pl.DataFrame:
        captured["lookback_days"] = lookback_days
        return pl.DataFrame({"symbol": ["A"], "date": [target_date]})

    monkeypatch.setattr(svc, "_load_enriched_history", fake_load)

    target = date(2026, 7, 15)
    context = svc.build_strategy_context(
        engine,
        target,
        ["custom_window"],
        current=pl.DataFrame({"symbol": ["A"], "date": [target]}),
    )
    assert captured["lookback_days"] == 22
    assert context.history is not None


def test_run_rejects_missing_custom_signal_column():
    """filter_history 策略 REQUIRED_FEATURES 引用未注入的 csg_ 列时,
    引擎给出明确中文报错, 而不是把 polars 缺列错抛成 500。"""
    from app.strategy.engine import StrategyDataContext

    sid = "custom_csg_missing"
    strategy = _filter_history_strategy(sid)
    strategy = dataclasses.replace(
        strategy,
        required_features=frozenset({"csg_oversold_macd_about_to_golden"}),
    )
    engine = _make_engine(strategy)

    target = date(2026, 7, 15)
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=target,
        current=None,
        history=pl.DataFrame({"symbol": ["A", "B"], "date": [target, target]}),
    )

    with pytest.raises(ValueError, match="csg_oversold_macd_about_to_golden"):
        engine.run(sid, context)


def test_run_ok_when_custom_signal_column_present():
    """csg_ 列已注入时正常运行 (回归: 不误伤)。"""
    from app.strategy.engine import StrategyDataContext

    sid = "custom_csg_ok"
    strategy = _filter_history_strategy(sid)
    strategy = dataclasses.replace(
        strategy,
        required_features=frozenset({"csg_oversold_macd_about_to_golden"}),
    )
    engine = _make_engine(strategy)

    target = date(2026, 7, 15)
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=target,
        current=None,
        history=pl.DataFrame({
            "symbol": ["A", "B"],
            "date": [target, target],
            "csg_oversold_macd_about_to_golden": [True, False],
        }),
    )

    result = engine.run(sid, context)
    assert result.strategy_id == sid
