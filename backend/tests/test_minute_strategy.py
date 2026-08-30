"""分钟策略 (minute_filter 后端) 测试。

覆盖:
- minute_red_streak 形态: 命中 / 不足根数不触发 / 最高K不红 / rank_by 两口径 /
  乱序输入 / 最高价并列取更晚K线
- 引擎加载校验: 只能声明 filter_minute_history、timeframes 必须且只能是 ["1m"]
- 引擎 1m 运行: enriched 联表基础过滤 (剔除ST / 股价区间)、entry hits、
  日线 context 拒绝
- ScreenerService 1m context: 当日分区优先、缺失回退最近分区、空库报错、
  非股票资产拒绝
"""
from __future__ import annotations

import datetime as _dt
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.services.screener import ScreenerService
from app.strategy.builtin import minute_red_streak
from app.strategy.engine import StrategyDataContext, StrategyEngine


def _bars(symbol: str, candles: list[tuple[float, float, float]], start_hour: int = 9) -> pl.DataFrame:
    """candles: (open, close, high) 序列, 时间从 start_hour:30 起每分钟一根。"""
    n = len(candles)
    base = datetime(2026, 8, 25, start_hour, 30)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": [base + _dt.timedelta(minutes=i) for i in range(n)],
        "open": [float(c[0]) for c in candles],
        "high": [float(c[2]) for c in candles],
        "low": [float(min(c[0], c[1])) for c in candles],
        "close": [float(c[1]) for c in candles],
        "volume": [100.0] * n,
        "amount": [10000.0] * n,
    })


# ── 形态 ────────────────────────────────────────────────────────────


def test_pattern_hits_five_red_of_seven_with_red_top_two():
    # 7根: 5红2绿, 绿K的最高价都压得比红K低 → 最高的两根(10.9/10.7)都是红
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.2, 10.1, 10.25),  # 绿 (低高点)
        (10.1, 10.4, 10.50),  # 红
        (10.4, 10.6, 10.70),  # 红 (次高)
        (10.6, 10.5, 10.65),  # 绿 (低高点)
        (10.5, 10.7, 10.80),  # 红
        (10.7, 10.8, 10.90),  # 红 (最高)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {})
    assert out["symbol"].to_list() == ["600000.SH"]
    row = out.row(0, named=True)
    assert row["red_count"] == 5
    assert row["top_red_count"] == 2
    assert row["close"] == 10.8


def test_pattern_insufficient_bars_never_triggers():
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", [(10.0, 10.2, 10.3)] * 6), {})
    assert out.is_empty()


def test_pattern_green_at_top_blocks_hit():
    # 5红, 但最高的一根是绿 (高开回落) → 最高两根不全红, 不触发
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.1, 10.4, 10.50),  # 红
        (10.3, 10.6, 10.70),  # 红
        (10.6, 10.5, 10.65),  # 绿 (低高点)
        (10.4, 10.5, 10.55),  # 红 (低高点)
        (11.5, 11.0, 12.00),  # 绿 (最高)
        (11.0, 11.4, 11.90),  # 红 (次高)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {})
    assert out.is_empty()


def test_pattern_rank_by_close_uses_close_not_high():
    # high 口径最高两根是绿K冲高; close 口径最高两根是红K → 仅 close 口径命中
    candles = [
        (10.0, 10.5, 10.60),  # 红
        (10.5, 10.9, 11.50),  # 绿 (high 最高, 并列)
        (10.9, 11.2, 11.40),  # 红
        (11.2, 11.3, 11.35),  # 红
        (11.3, 11.4, 11.45),  # 红 (close 次高)
        (11.4, 11.1, 11.50),  # 绿 (high 最高, 并列)
        (11.1, 11.5, 11.55),  # 红 (close 最高)
    ]
    by_high = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {})
    by_close = minute_red_streak.filter_minute_history(
        _bars("600000.SH", candles), {"rank_by_close": True}
    )
    assert by_high.is_empty()
    assert by_close["symbol"].to_list() == ["600000.SH"]


def test_pattern_sorts_unordered_input_by_datetime():
    bars = pl.concat([
        _bars("600000.SH", [(10.0, 10.2, 10.30)]),
        _bars("600000.SH", [
            (10.2, 10.1, 10.25), (10.1, 10.4, 10.50), (10.4, 10.6, 10.70),
            (10.6, 10.5, 10.65), (10.5, 10.7, 10.80), (10.7, 10.8, 10.90),
        ]),
    ]).sample(fraction=1.0, shuffle=True, seed=7)
    out = minute_red_streak.filter_minute_history(bars, {})
    assert out["symbol"].to_list() == ["600000.SH"]
    assert out.row(0, named=True)["close"] == 10.8  # 最后一根(时间最大)的收盘


def test_pattern_three_way_high_tie_prefers_later_bars():
    # 三根 high 并列最高: 更早的绿K应被更晚的两根红K挤出 top2 → 命中
    # (若并列取更早, top2 = {红, 绿} → 不命中; 该测试固定 "同值取更晚" 契约)
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.1, 10.4, 10.50),  # 红
        (10.2, 10.1, 10.25),  # 绿 (低高点)
        (10.3, 10.6, 10.70),  # 红
        (10.8, 10.5, 10.90),  # 绿 (并列最高, 最早 → 被 top2 排除)
        (10.5, 10.6, 10.90),  # 红 (并列最高, 中间)
        (10.6, 10.8, 10.90),  # 红 (并列最高, 最晚)
    ]
    out = minute_red_streak.filter_minute_history(_bars("600000.SH", candles), {})
    assert out["symbol"].to_list() == ["600000.SH"]
    assert out.row(0, named=True)["top_red_count"] == 2


def test_pattern_min_red_threshold_respected():
    # 4红3绿, 最高的两根红 → min_red=5 不命中, min_red=4 命中
    candles = [
        (10.0, 10.2, 10.30),  # 红
        (10.2, 10.1, 10.25),  # 绿
        (10.1, 10.4, 10.50),  # 红
        (10.4, 10.3, 10.45),  # 绿
        (10.3, 10.6, 10.70),  # 红
        (10.6, 10.5, 10.65),  # 绿
        (10.5, 10.8, 10.90),  # 红
    ]
    bars = _bars("600000.SH", candles)
    assert minute_red_streak.filter_minute_history(bars, {"min_red": 5}).is_empty()
    assert not minute_red_streak.filter_minute_history(bars, {"min_red": 4}).is_empty()


# ── 引擎加载与运行 ──────────────────────────────────────────────────


def test_builtin_minute_strategy_loads_with_minute_filter_backend():
    engine = StrategyEngine(
        strategy_dirs=[Path(__file__).resolve().parent.parent / "app" / "strategy" / "builtin"]
    )
    assert not [e for e in engine.load_errors() if "minute" in e["file"]]
    s = engine.get("minute_red_streak")
    assert s.execution_backend == "minute_filter"
    assert s.filter_minute_history_fn is not None
    assert s.meta["timeframes"] == ["1m"]


def _minute_code(sid: str, timeframes: str = '["1m"]', extra: str = "") -> str:
    return f'''import polars as pl
META = {{"id": "{sid}", "name": "{sid}", "asset_types": ["stock"], "timeframes": {timeframes}}}
EXECUTION_BACKEND = "minute_filter"
{extra}
def filter_minute_history(df, params):
    return df.group_by("symbol").agg(
        close=pl.col("close").max(), last_datetime=pl.col("datetime").max()
    )
'''


def test_minute_filter_backend_validation(tmp_path):
    (tmp_path / "ok.py").write_text(_minute_code("m_ok"))
    (tmp_path / "bad_filter.py").write_text(
        _minute_code("m_bad1", extra="def filter(df, params):\n    return pl.lit(True)")
    )
    (tmp_path / "bad_tf.py").write_text(_minute_code("m_bad2", timeframes='["1d", "1m"]'))
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    ids = {m["id"] for m in engine.list_strategies(include_research=True)}
    assert "m_ok" in ids
    assert "m_bad1" not in ids
    assert "m_bad2" not in ids
    assert any("only filter_minute_history" in e["error"] for e in engine.load_errors())
    assert any("timeframes" in e["error"] for e in engine.load_errors())


def test_minute_context_run_applies_enriched_basic_filter(tmp_path):
    (tmp_path / "m_basic.py").write_text(_minute_code("m_basic"))
    engine = StrategyEngine(strategy_dirs=[tmp_path])

    hist = pl.concat([
        _bars("600001.SH", [(10.0, 20.0, 25.0)] * 7),   # 命中, 收盘 20
        _bars("600002.SH", [(10.0, 20.0, 25.0)] * 7),   # 命中但 ST → 剔除
        _bars("600003.SH", [(10.0, 20.0, 25.0)] * 7),   # 命中
        _bars("600004.SH", [(100.0, 200.0, 250.0)] * 7), # 命中但收盘 200 → 超上限剔除
    ])
    current = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "name": ["正常股", "ST垃圾", "正常股2", "高价股"],
        "total_shares": [1e8, 1e8, 1e8, 1e8],
        "float_shares": [5e7, 5e7, 5e7, 5e7],
        "amount": [3e8, 3e8, 3e8, 3e8],
        "change_pct": [0.01, 0.01, 0.01, 0.01],
    })
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1m",
        as_of=date(2026, 8, 25),
        current=current,
        history=hist,
    )
    result = engine.run(
        "m_basic", context, overrides={"basic_filter": {"price_max": 150.0}}
    )
    symbols = {r["symbol"] for r in result.rows}
    assert symbols == {"600001.SH", "600003.SH"}
    assert all("name" in r for r in result.rows)  # enriched 列已联表
    assert {h["symbol"] for h in result.entry_signal_hits} == symbols


def test_minute_strategy_rejects_daily_context(tmp_path):
    (tmp_path / "m_daily.py").write_text(_minute_code("m_daily"))
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 8, 25),
        current=pl.DataFrame({"symbol": ["600001.SH"]}),
    )
    try:
        engine.run("m_daily", context)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "timeframe" in str(e)


# ── ScreenerService 1m context ──────────────────────────────────────


class _FakeMinuteRepo:
    def __init__(self, partitions: dict[date, pl.DataFrame]):
        self.partitions = partitions

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        frames = [self.partitions[d] for d in dates if d in self.partitions]
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames).filter(pl.col("symbol").is_in(symbols))

    def latest_minute_date_global(self):
        return max(self.partitions) if self.partitions else None


def _svc(partitions: dict[date, pl.DataFrame], asset_type: str = "stock") -> ScreenerService:
    return ScreenerService(_FakeMinuteRepo(partitions), asset_type=asset_type)  # type: ignore[arg-type]


def test_minute_context_prefers_as_of_partition():
    d1, d2 = date(2026, 8, 24), date(2026, 8, 25)
    svc = _svc({
        d1: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 3),
        d2: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 4),
    })
    ctx = svc.build_strategy_context(
        None, d1, [], timeframe="1m",
        current=pl.DataFrame({"symbol": ["600001.SH"], "name": ["x"]}),
    )
    assert ctx.history.height == 3  # as_of 当日分区, 不取更新的 d2
    assert ctx.timeframe == "1m"


def test_minute_context_falls_back_to_latest_partition():
    d1, d2 = date(2026, 8, 24), date(2026, 8, 25)
    svc = _svc({
        d1: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 3),
        d2: _bars("600001.SH", [(10.0, 10.2, 10.3)] * 4),
    })
    ctx = svc.build_strategy_context(
        None, date(2026, 8, 20), [], timeframe="1m",
        current=pl.DataFrame({"symbol": ["600001.SH"]}),
    )
    assert ctx.history.height == 4  # 回退到最近分区 d2


def test_minute_context_empty_store_raises_with_guidance():
    svc = _svc({})
    try:
        svc.build_strategy_context(
            None, date(2026, 8, 25), [], timeframe="1m",
            current=pl.DataFrame({"symbol": ["600001.SH"]}),
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "分钟K" in str(e)


def test_minute_context_rejects_non_stock_asset():
    svc = _svc({date(2026, 8, 25): _bars("510300.SH", [(10.0, 10.2, 10.3)] * 3)}, asset_type="etf")
    try:
        svc.build_strategy_context(
            None, date(2026, 8, 25), [], timeframe="1m",
            current=pl.DataFrame({"symbol": ["510300.SH"]}),
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "A 股" in str(e)
