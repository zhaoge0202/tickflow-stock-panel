"""扩充批次 (2026-09-05) 16 个新虚拟因子的数值正确性测试。

合成单标的日频面板, 黄金值由 numpy 独立重算 (不复制实现),
覆盖: 公式口径 / 无前视 (min_samples) / 除零 fail-closed / 列代数型因子。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.strategy.scoring import materialize_scoring_columns

N_DAYS = 250
DATES = [date(2025, 1, 1) + timedelta(days=i) for i in range(N_DAYS)]
T = np.arange(N_DAYS, dtype=float)

# 自然波动收益序列 (趋势 + 正弦): 避免等比价格的常数收益让波动率退化为浮点噪声
DAILY_RET = 0.002 + 0.01 * np.sin(T / 9.0)
CLOSE = 100.0 * np.cumprod(1.0 + DAILY_RET)
OPEN = np.roll(CLOSE, 1) * 1.005
OPEN[0] = 99.0 * 1.005  # 隔夜跳空 +0.5%
PREV_CLOSE = np.roll(CLOSE, 1)
PREV_CLOSE[0] = 99.0
VOLUME = 1_000_000 + 500.0 * T                           # 量能缓增
TURNOVER = VOLUME / 200_000_000.0                        # 流通股本 2 亿股
RET = np.concatenate([[np.nan], CLOSE[1:] / CLOSE[:-1] - 1.0])

# 列代数型因子的依赖列直接给黄金友好值
RSI = 50.0 + 10.0 * np.sin(T / 7.0)
MOM20 = np.concatenate([np.full(20, np.nan), CLOSE[20:] / CLOSE[:-20] - 1.0])
MOM60 = np.concatenate([np.full(60, np.nan), CLOSE[60:] / CLOSE[:-60] - 1.0])
KDJ_K = 50.0 + 15.0 * np.cos(T / 11.0)
KDJ_D = 50.0 + 5.0 * np.sin(T / 13.0)
AMPLITUDE = 0.02 + 0.0001 * T


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["TEST"] * N_DAYS,
        "date": DATES,
        "open": OPEN,
        "high": np.maximum(OPEN, CLOSE) * 1.01,
        "low": np.minimum(OPEN, CLOSE) * 0.99,
        "close": CLOSE,
        "prev_close": PREV_CLOSE,
        "volume": VOLUME,
        "amount": VOLUME * CLOSE * 100.0,
        "turnover_rate": TURNOVER,
        "rsi_14": RSI,
        "momentum_20d": MOM20,
        "momentum_60d": MOM60,
        "kdj_k": KDJ_K,
        "kdj_d": KDJ_D,
        "amplitude": AMPLITUDE,
    })


def _col(frame: pl.DataFrame, name: str) -> np.ndarray:
    return frame[name].to_numpy()


def _materialize(names: list[str]) -> pl.DataFrame:
    return materialize_scoring_columns(_panel(), names)


def test_momentum_120d_golden() -> None:
    frame = _materialize(["momentum_120d"])
    got = _col(frame, "momentum_120d")
    golden = np.full(N_DAYS, np.nan)
    golden[120:] = CLOSE[120:] / CLOSE[:-120] - 1.0
    assert np.allclose(got[121:], golden[121:], atol=1e-12)
    assert np.isnan(got[:120]).all()  # 无前视: 前 120 根必为空 (min_samples)


def test_mom_accel_and_kdj_diff_column_algebra() -> None:
    frame = _materialize(["mom_accel_20_60", "kdj_kd_diff", "rsi_14_delta_5d"])
    assert np.allclose(_col(frame, "mom_accel_20_60"), MOM20 - MOM60, equal_nan=True)
    assert np.allclose(_col(frame, "kdj_kd_diff"), KDJ_K - KDJ_D, atol=1e-12)
    delta = np.full(N_DAYS, np.nan)
    delta[5:] = RSI[5:] - RSI[:-5]
    assert np.allclose(_col(frame, "rsi_14_delta_5d")[6:], delta[6:], atol=1e-12)


def test_overnight_and_intraday_decomposition() -> None:
    frame = _materialize(["overnight_ret_20d", "intraday_ret_20d"])
    overnight_daily = OPEN / PREV_CLOSE - 1.0
    intraday_daily = CLOSE / OPEN - 1.0
    # 后向滚动窗: got[i] = sum(daily[i-19 .. i]); convolve('valid')[k] = sum(daily[k..k+19])
    # → got[i] 对应 valid[i-19], 从 i=21 起比对 (跳过合成首日 prev_close 特例)
    golden_on = np.convolve(overnight_daily, np.ones(20), "valid")[2:]
    golden_in = np.convolve(intraday_daily, np.ones(20), "valid")[2:]
    got_on = _col(frame, "overnight_ret_20d")
    got_in = _col(frame, "intraday_ret_20d")
    assert np.allclose(got_on[21:], golden_on, atol=1e-10)
    assert np.allclose(got_in[21:], golden_in, atol=1e-10)
    # 恒等式: sum(隔夜) + sum(日内) ≈ sum(全天收益); 精确差为每日交叉项 on*in
    # (跳空0.5% x 日内~1%, 20日累计 ~1e-3), 故用 5e-3 容差
    total = got_on[21:] + got_in[21:]
    golden_total = np.convolve(CLOSE / PREV_CLOSE - 1.0, np.ones(20), "valid")[2:]
    assert np.allclose(total, golden_total, atol=5e-3)


def test_downside_vol_only_counts_negative_side() -> None:
    frame = _materialize(["downside_vol_20d"])
    got = _col(frame, "downside_vol_20d")[21:]
    for i, day in enumerate(range(21, N_DAYS)):
        window = np.minimum(RET[day - 19: day + 1], 0.0)
        golden = np.sqrt(np.mean(window ** 2))
        assert got[i] == pytest.approx(golden, abs=1e-12), f"day index {day}"


def test_obv_trend_bounded_and_golden() -> None:
    frame = _materialize(["obv_trend_20d"])
    got = _col(frame, "obv_trend_20d")
    for day in range(21, N_DAYS, 25):
        window_ret = RET[day - 19: day + 1]
        window_vol = VOLUME[day - 19: day + 1]
        signed = np.sign(window_ret) * window_vol
        golden = signed.sum() / (window_vol.mean() * 20.0)
        assert got[day] == pytest.approx(golden, abs=1e-9), f"day index {day}"
    valid = got[~np.isnan(got)]
    assert (np.abs(valid) <= 1.0 + 1e-12).all()  # 有界 [-1, 1]


def test_log_float_mv_golden_and_fail_closed() -> None:
    frame = _materialize(["log_float_mv"])
    got = _col(frame, "log_float_mv")
    golden = np.log(CLOSE * VOLUME / TURNOVER)
    assert np.allclose(got, golden, atol=1e-10)  # = ln(流通市值), 股本=2亿
    # 换手率为 0 → None (fail-closed, 不产生 inf)
    broken = _panel().with_columns(pl.lit(0.0).alias("turnover_rate"))
    out = materialize_scoring_columns(broken, ["log_float_mv"])
    assert out["log_float_mv"].is_null().all()


def test_position_240d_and_distance_to_high() -> None:
    frame = _materialize(["position_240d", "distance_to_high_240d"])
    pos = _col(frame, "position_240d")
    dist = _col(frame, "distance_to_high_240d")
    for day in (241, 245, N_DAYS - 1):
        window = CLOSE[day - 239: day + 1]
        golden_pos = (CLOSE[day] - window.min()) / (window.max() - window.min())
        assert pos[day] == pytest.approx(golden_pos, abs=1e-12), f"pos day {day}"
        assert dist[day] == pytest.approx(CLOSE[day] / window.max() - 1.0, abs=1e-12)
    assert np.isnan(pos[:239]).all()  # 无前视: 240 日窗在索引 239 才首次有效


def test_vol_regime_amplitude_trend_turnover_stats() -> None:
    frame = _materialize(["vol_regime_5_60", "amplitude_trend_20_60", "turnover_mean_20d", "turnover_std_20d"])
    vr = _col(frame, "vol_regime_5_60")
    at = _col(frame, "amplitude_trend_20_60")
    tm = _col(frame, "turnover_mean_20d")
    ts = _col(frame, "turnover_std_20d")
    for day in (61, 120, N_DAYS - 1):
        fast = np.std(RET[day - 4: day + 1], ddof=1)
        slow = np.std(RET[day - 59: day + 1], ddof=1)
        assert vr[day] == pytest.approx(fast / slow, rel=1e-9, abs=1e-12), f"vr day {day}"
        a_fast = AMPLITUDE[day - 19: day + 1].mean()
        a_slow = AMPLITUDE[day - 59: day + 1].mean()
        assert at[day] == pytest.approx(a_fast / a_slow - 1.0, rel=1e-9, abs=1e-12)
        t_window = TURNOVER[day - 19: day + 1]
        assert tm[day] == pytest.approx(t_window.mean(), rel=1e-12)
        assert ts[day] == pytest.approx(t_window.std(ddof=1) / t_window.mean(), rel=1e-9)


def test_amount_mean_20d_unit_is_yi() -> None:
    frame = _materialize(["amount_mean_20d"])
    got = _col(frame, "amount_mean_20d")
    day = N_DAYS - 1
    golden = (VOLUME[day - 19: day + 1] * CLOSE[day - 19: day + 1] * 100.0).mean() / 1e8
    assert got[day] == pytest.approx(golden, rel=1e-12)
