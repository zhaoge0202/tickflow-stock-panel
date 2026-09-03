"""因子回测默认区间回归测试。

覆盖 issue #202: `/api/backtest/factor/run` 与 `/api/backtest/factor/batch`
在省略 `start` 时错误使用了策略默认区间(3 年)而非因子默认区间(180 天)。

只关注「起始日解析」这一关注点, 用替身 Service 捕获传入配置, 不做真实数据加载。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import ClassVar

import pytest

from app.api import backtest as bt


@dataclass
class _DummyResult:
    ok: bool = True


class _CapturingService:
    """替身 FactorBacktestService: 捕获配置, 不触真实引擎。"""

    captured: ClassVar[list] = []

    def __init__(self, engine):
        pass

    def run(self, cfg):
        _CapturingService.captured.append(cfg)
        return _DummyResult()

    def run_batch(self, cfg):
        _CapturingService.captured.append(cfg)
        return _DummyResult()


class _Req:
    """占位 request; _get_engine 已被 patch, 不会真正访问其属性。"""


@pytest.fixture
def patched(monkeypatch):
    _CapturingService.captured = []
    monkeypatch.setattr(bt, "_get_engine", lambda request: object())
    # factor_run / factor_batch 内部 `from app.backtest.factor import FactorBacktestService`
    # 读取的是模块属性, patch 源模块即可命中。
    import app.backtest.factor as factor_mod
    monkeypatch.setattr(factor_mod, "FactorBacktestService", _CapturingService)
    # 隔离守卫, 单测「起始日解析」。
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda start, end: None)
    return _CapturingService


def test_factor_run_omitted_start_uses_factor_default(patched):
    end = date(2026, 8, 24)
    bt.factor_run(bt.FactorBacktestRequest(factor_name="momentum_5d", end=end), _Req())
    cfg = patched.captured[-1]
    assert cfg.start == end - timedelta(days=bt.FACTOR_DEFAULT_DAYS)
    # 回归保护: 不得再退回策略默认的 3 年。
    assert cfg.start != end - timedelta(days=bt.STRATEGY_DEFAULT_DAYS)


def test_factor_batch_omitted_start_uses_factor_default(patched):
    end = date(2026, 8, 24)
    bt.factor_batch(bt.FactorBatchRequest(factor_names=["momentum_5d"], end=end), _Req())
    cfg = patched.captured[-1]
    assert cfg.start == end - timedelta(days=bt.FACTOR_DEFAULT_DAYS)
    assert cfg.start != end - timedelta(days=bt.STRATEGY_DEFAULT_DAYS)


def test_factor_run_explicit_null_start_means_all_history(patched):
    """显式传 start=null 语义不变: 代表全部历史。"""
    end = date(2026, 8, 24)
    bt.factor_run(bt.FactorBacktestRequest(factor_name="momentum_5d", start=None, end=end), _Req())
    assert patched.captured[-1].start == date(1900, 1, 1)


def test_factor_run_explicit_start_passthrough(patched):
    """显式日期原样透传。"""
    end = date(2026, 8, 24)
    bt.factor_run(
        bt.FactorBacktestRequest(factor_name="momentum_5d", start=date(2026, 1, 1), end=end),
        _Req(),
    )
    assert patched.captured[-1].start == date(2026, 1, 1)
