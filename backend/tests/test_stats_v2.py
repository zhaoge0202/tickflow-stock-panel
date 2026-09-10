"""metrics_v2 统计函数测试 (P3) — 黄金参考向量 + 性质断言。

NW t 的黄金值由测试内的独立第二实现 (显式循环求 Bartlett 加权长方差) 推导,
与 stats_v2 向量化实现互为对拍。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.backtest.stats_v2 import (
    _normal_ppf,
    bh_fdr_qvalues,
    deflated_sharpe_psr,
    expected_max_sharpe,
    naive_t,
    newey_west_t,
    normal_two_sided_p,
)


def _nw_t_reference(values: list[float], lag: int) -> float:
    """独立第二实现: 显式循环按定义计算 Bartlett 核 HAC t 值。"""
    n = len(values)
    mean = sum(values) / n
    centered = [value - mean for value in values]
    gamma = [
        sum(centered[i] * centered[i + lag_i] for i in range(n - lag_i)) / n
        for lag_i in range(lag + 1)
    ]
    long_var = gamma[0]
    for lag_i in range(1, lag + 1):
        long_var += 2.0 * (1.0 - lag_i / (lag + 1)) * gamma[lag_i]
    se = math.sqrt(long_var / n)
    return mean / se


def test_newey_west_matches_reference() -> None:
    rng = np.random.default_rng(42)
    values = np.cumsum(rng.normal(0, 0.01, 60)).tolist()  # 高自相关
    for lag in (1, 3, 5):
        result = newey_west_t(values, lag)
        assert result is not None
        t_stat, mean, se = result
        assert t_stat == pytest.approx(_nw_t_reference(values, lag), rel=1e-9)
        assert mean == pytest.approx(float(np.mean(values)))
        assert se > 0


def test_newey_west_deflates_autocorrelated_t() -> None:
    # 强正自相关序列: NW t 的绝对值必须小于朴素 t (自相关被正确惩罚)
    rng = np.random.default_rng(7)
    phi = 0.9
    values, last = [], 0.0
    for shock in rng.normal(0, 0.01, 500):
        last = phi * last + shock
        values.append(last)
    t_naive = naive_t(values)
    result = newey_west_t(values, lag=5)
    assert t_naive is not None and result is not None
    assert abs(result[0]) < abs(t_naive)


def test_newey_west_insufficient_samples() -> None:
    assert newey_west_t([0.1, 0.2], lag=1) is None
    assert newey_west_t([], lag=1) is None
    assert newey_west_t([1.0] * 20, lag=1) is None  # 零方差


def test_bh_fdr_golden() -> None:
    # 经典 BH 示例 (Wikipedia): q = [.005, .02, .042, .042, .042]
    pvalues = [0.001, 0.008, 0.039, 0.041, 0.042]
    assert bh_fdr_qvalues(pvalues) == pytest.approx([0.005, 0.02, 0.042, 0.042, 0.042])
    # 乱序输入: q 值跟随原位置 (m=3: .042→r3 raw .042; .001→.003; .039→min(.0585,.042)=.042)
    assert bh_fdr_qvalues([0.042, 0.001, 0.039]) == pytest.approx([0.042, 0.003, 0.042])
    # None 透传
    assert bh_fdr_qvalues([None, 0.05]) == [None, 0.05]


def test_normal_p_and_ppf_inverse() -> None:
    assert normal_two_sided_p(1.959964) == pytest.approx(0.05, abs=1e-6)
    assert normal_two_sided_p(0.0) == pytest.approx(1.0)
    assert _normal_ppf(0.975) == pytest.approx(1.959964, abs=1e-6)
    assert _normal_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(ValueError):
        _normal_ppf(0.0)


def test_expected_max_sharpe_monotone() -> None:
    assert expected_max_sharpe(1, 0.04) == 0.0  # 单试验不校正
    assert expected_max_sharpe(10, 0.0) == 0.0
    # 试验数越多期望最大夏普越高 (越难超越)
    em_10 = expected_max_sharpe(10, 0.04)
    em_100 = expected_max_sharpe(100, 0.04)
    assert 0 < em_10 < em_100


def test_deflated_sharpe_psr() -> None:
    # 无偏斜无超额峰度时退化为 Φ(SR * sqrt(n-1))
    probability = deflated_sharpe_psr(sharpe=0.1, n_obs=2500, skewness=0.0, kurtosis=3.0, expected_max_sharpe=0.0)
    assert probability == pytest.approx(0.5 * (1 + math.erf(0.1 * math.sqrt(2499) / math.sqrt(2))))
    # 校正项抬高分母会降低 PSR
    penalized = deflated_sharpe_psr(0.1, 2500, skewness=0.0, kurtosis=10.0)
    assert penalized < probability
    # EM 校正降低显著性
    deflated = deflated_sharpe_psr(0.1, 2500, 0.0, 3.0, expected_max_sharpe=0.08)
    assert deflated < probability
    assert deflated_sharpe_psr(0.1, 3) is None  # 样本不足
