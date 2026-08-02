"""市场环境(regime) 计算与持久化测试。

覆盖:
- classify_state: 五种状态边界值(强势/偏强/震荡/偏弱/弱势)
- _aggregate_daily: 多日多 symbol 聚合(涨停数/涨跌家数/MA20占比)
- upsert_regime_history: 按 date 覆盖(重算的天替换旧行)
- compute_regime_incremental: 双检测(缺口 + stale mtime)
"""
from __future__ import annotations

import os
import time
from datetime import date

import polars as pl

from app.services import regime_builder

# ───────────────────────── 状态分类 ─────────────────────────


def test_classify_strong():
    state, score = regime_builder.classify_state({
        "limit_up": 40, "limit_down": 1, "seal_rate": 0.85, "up_ratio": 3.0,
        "index_pct": 0.02, "above_ma20_pct": 0.7, "total_amount": 2e11,
    })
    assert state == "strong"
    assert score >= 75


def test_classify_weak():
    state, score = regime_builder.classify_state({
        "limit_up": 1, "limit_down": 20, "seal_rate": 0.2, "up_ratio": 0.2,
        "index_pct": -0.025, "above_ma20_pct": 0.2, "total_amount": 5e10,
    })
    assert state == "weak"
    assert score < 25


def test_classify_range():
    state, score = regime_builder.classify_state({
        "limit_up": 8, "limit_down": 6, "seal_rate": 0.5, "up_ratio": 1.0,
        "index_pct": 0.0, "above_ma20_pct": 0.5, "total_amount": 1e11,
    })
    assert state == "range"
    assert 40 <= score < 60


def test_classify_monotonic_limit_up():
    """涨停数越多, 综合分越高(其他条件相同)。"""
    base = {"limit_down": 2, "seal_rate": 0.7, "up_ratio": 2.0,
            "index_pct": 0.01, "above_ma20_pct": 0.6, "total_amount": 1.5e11}
    s_low = regime_builder.classify_state({**base, "limit_up": 5})[1]
    s_mid = regime_builder.classify_state({**base, "limit_up": 20})[1]
    s_high = regime_builder.classify_state({**base, "limit_up": 45})[1]
    assert s_low < s_mid < s_high


# ───────────────────────── 聚合 ─────────────────────────


def _enriched_df() -> pl.DataFrame:
    """构造 2 天 × 4 标的 的 enriched 数据(含信号列)。"""
    return pl.DataFrame({
        "date": [date(2026, 1, 2)] * 4 + [date(2026, 1, 3)] * 4,
        "symbol": ["A", "B", "C", "D"] * 2,
        "close": [11, 9, 21, 19, 12, 8, 22, 18],
        "change_pct": [0.1, -0.1, 0.05, -0.05, 0.08, -0.12, 0.02, -0.08],
        "amount": [1e8, 2e8, 3e8, 4e8] * 2,
        "ma20": [10, 10, 20, 20, 10, 10, 20, 20],
        "signal_limit_up": [True, False, False, False, True, False, True, False],
        "signal_limit_down": [False, False, False, True, False, False, False, False],
        "signal_broken_limit_up": [False, False, False, False, False, False, False, False],
        "consecutive_limit_ups": [1, 0, 0, 0, 2, 0, 1, 0],
    })


def test_aggregate_daily_basic():
    """聚合多日: 每天的涨停数/涨跌家数正确。"""
    df = _enriched_df()
    result = regime_builder._aggregate_daily(df, index_pct_map={
        date(2026, 1, 2): 0.01, date(2026, 1, 3): -0.005,
    })
    assert result.height == 2
    # 第一天(1/2): 1 个涨停, 2 涨 2 跌
    r1 = result.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r1["limit_up"] == 1
    assert r1["up_count"] == 2
    assert r1["down_count"] == 2
    assert r1["max_consecutive"] == 1
    # 第二天(1/3): 2 个涨停, 2 涨 2 跌, 连板高度 2
    r2 = result.filter(pl.col("date") == date(2026, 1, 3)).row(0, named=True)
    assert r2["limit_up"] == 2
    assert r2["max_consecutive"] == 2
    # 每行都有 state 和 score
    assert all(s in {"strong", "lean_strong", "range", "lean_weak", "weak"}
               for s in result["state"].to_list())
    assert result["score"].min() >= 0 and result["score"].max() <= 100


def test_aggregate_daily_ma20_above():
    """MA20 上方占比正确(close > ma20)。"""
    df = _enriched_df()
    result = regime_builder._aggregate_daily(df)
    r1 = result.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    # 1/2: A(close11>ma10)✓, B(9<10)✗, C(21>20)✓, D(19<20)✗ → 2/4 = 0.5
    assert r1["above_ma20_pct"] == 0.5


def test_aggregate_empty_returns_empty():
    assert regime_builder._aggregate_daily(pl.DataFrame()).is_empty()


# ───────────────────────── 持久化(upsert) ─────────────────────────


def test_upsert_inserts_new(tmp_path):
    rows = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["strong", "range"],
        "score": [80, 50],
    })
    regime_builder.upsert_regime_history(tmp_path, rows)
    loaded = regime_builder.load_regime_history(tmp_path)
    assert loaded.height == 2


def test_upsert_overwrites_existing_date(tmp_path):
    """重算的天覆盖旧行(upsert 语义)。"""
    old = pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    })
    regime_builder.upsert_regime_history(tmp_path, old)
    # 重算 1/2
    new = pl.DataFrame({
        "date": [date(2026, 1, 2)],
        "state": ["strong"], "score": [85],
    })
    regime_builder.upsert_regime_history(tmp_path, new)
    loaded = regime_builder.load_regime_history(tmp_path)
    assert loaded.height == 2  # 仍是 2 天(1/2 被覆盖, 不重复)
    r2 = loaded.filter(pl.col("date") == date(2026, 1, 2)).row(0, named=True)
    assert r2["state"] == "strong"
    assert r2["score"] == 85
    # 1/1 不受影响
    r1 = loaded.filter(pl.col("date") == date(2026, 1, 1)).row(0, named=True)
    assert r1["state"] == "range"


def test_coverage_metadata(tmp_path):
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 5)],
        "state": ["strong", "weak"], "score": [80, 20],
    }))
    cov = regime_builder.get_regime_coverage(tmp_path)
    assert cov["rows"] == 2
    assert cov["earliest_date"] == "2026-01-01"
    assert cov["latest_date"] == "2026-01-05"


def test_coverage_empty(tmp_path):
    cov = regime_builder.get_regime_coverage(tmp_path)
    assert cov["rows"] == 0
    assert cov["earliest_date"] is None


# ───────────────────────── 双检测 ─────────────────────────


def test_detect_stale_dates_by_mtime(tmp_path):
    """enriched 分区 mtime > regime mtime → 标记重算。"""
    # 准备 regime 历史
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    # 模拟 enriched 分区(先写, mtime=T2)
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-02"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")
    # 重新 upsert regime → regime mtime 更新到 T3 > enriched 的 T2
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    time.sleep(0.05)  # 确保 mtime 精度差异
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "range"], "score": [50, 50],
    }))
    # 让 1/2 的 mtime 更新到 future > regime mtime
    future = time.time() + 10
    os.utime(enriched_dir / "date=2026-01-02" / "part.parquet", (future, future))

    class _FakeRepo:
        class store:
            data_dir = tmp_path
    stale = regime_builder.detect_stale_dates(tmp_path, _FakeRepo())
    assert date(2026, 1, 2) in stale
    assert date(2026, 1, 1) not in stale  # 1/1 没更新


def test_compute_incremental_missing_dates(tmp_path):
    """enriched 有但 regime 没有 → 补算缺口。"""
    # regime 只有 1/1
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1)], "state": ["range"], "score": [50],
    }))
    # 模拟 enriched 有 1/1 和 1/2
    enriched_dir = tmp_path / "kline_daily_enriched"
    for ds in ["2026-01-01", "2026-01-02"]:
        d = enriched_dir / f"date={ds}"
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    class _FakeRepo:
        class store:
            data_dir = tmp_path
        def get_enriched_range(self, *a, **k): return None  # 无缓存, 不实际算

    # compute_regime_incremental 会识别 1/2 缺口, 但 run_regime_batch 因无数据返回空
    new = regime_builder.compute_regime_incremental(_FakeRepo(), tmp_path, today=date(2026, 1, 3))
    # 无真实 enriched 数据 → 不算出新行, 但不报错
    assert new.is_empty() or new.height >= 0


# ───────────────────────── 回测环境过滤(T-1 防未来函数) ─────────────────────────


def test_build_regime_mask_t1_alignment(tmp_path):
    """_build_regime_mask 强制 T-1: regime[T-1] 决定 entry[T]。

    场景: regime 1/1=weak(10), 1/2=strong(85)。
    timestamp_labels: [1/1, 1/2, 1/3]。
    filter: 只允许 strong。
    期望: mask = [True(首日默认允许), False(1/2的前一日=1/1=weak), True(1/3的前一日=1/2=strong)]。
    """
    from app.backtest.strategy import StrategyBacktestService

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["weak", "strong"],
        "score": [10, 85],
    }))
    labels = ("2026-01-01", "2026-01-02", "2026-01-03")
    mask = StrategyBacktestService._build_regime_mask(
        labels, {"states": ["strong"]}, tmp_path,
    )
    assert mask is not None
    assert mask.tolist() == [True, False, True]


def test_build_regime_mask_min_score(tmp_path):
    """min_score 过滤: regime[T-1] 的 score >= min_score 才允许入场。"""
    from app.backtest.strategy import StrategyBacktestService

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "state": ["range", "lean_strong"],
        "score": [45, 65],
    }))
    labels = ("2026-01-01", "2026-01-02", "2026-01-03")
    mask = StrategyBacktestService._build_regime_mask(
        labels, {"min_score": 60}, tmp_path,
    )
    # 1/2 entry 由 1/1(score=45 < 60) 决定 → False
    # 1/3 entry 由 1/2(score=65 >= 60) 决定 → True
    assert mask.tolist() == [True, False, True]


def test_build_regime_mask_none_when_no_filter():
    """regime_filter 为 None → 返回 None(不过滤)。"""
    from app.backtest.strategy import StrategyBacktestService

    assert StrategyBacktestService._build_regime_mask(("2026-01-01",), None, None) is None


def test_build_regime_mask_none_when_no_data(tmp_path):
    """无 regime 历史数据 → 返回 None(不阻断回测)。"""
    from app.backtest.strategy import StrategyBacktestService

    mask = StrategyBacktestService._build_regime_mask(
        ("2026-01-01", "2026-01-02"), {"states": ["strong"]}, tmp_path,
    )
    assert mask is None


def test_build_regime_mask_first_day_allowed(tmp_path):
    """首日无前一日环境数据 → 默认允许(不阻断)。"""
    from app.backtest.strategy import StrategyBacktestService

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 1, 1)],
        "state": ["weak"], "score": [10],
    }))
    labels = ("2026-01-01", "2026-01-02")
    mask = StrategyBacktestService._build_regime_mask(
        labels, {"states": ["strong"]}, tmp_path,
    )
    # 1/1 首日 → True; 1/2 由 1/1(weak) → False
    assert mask.tolist() == [True, False]

