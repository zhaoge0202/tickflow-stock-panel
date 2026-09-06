"""盘中信号(分钟K)测试 — 特征帧 / 编译 / 旧 4 信号等价 / 引擎注入 / 回放。

口径单源的核心保证: 测试内复刻 v1 盘中评估器的累计循环作为黄金参照,
新评估器(v2, 特征帧 + 表达式)必须与它对任意序列产出完全一致的触发集合。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.strategy import custom_signals
from app.strategy.intraday_features import build_feature_frame
from app.strategy.intraday_signals import IntradaySignalEvaluator, uses_intraday_signals

SYMBOL = "000001.SZ"
DAY = date(2026, 9, 4)


def _bars(
    prices: list[float],
    volumes: list[float] | None = None,
    start: datetime = datetime(2026, 9, 4, 9, 30),
    symbol: str = SYMBOL,
) -> pl.DataFrame:
    """构造规范的上午分钟序列: volume 单位为手, amount = close*volume*100(元)。"""
    volumes = volumes or [100.0] * len(prices)
    # 跳过午休: 09:30+120 根后进入 13:00
    times = []
    for i in range(len(prices)):
        if i < 120:
            times.append(start + timedelta(minutes=i))
        else:
            times.append(datetime(start.year, start.month, start.day, 13, 0) + timedelta(minutes=i - 120))
    return pl.DataFrame({
        "symbol": [symbol] * len(prices),
        "datetime": times,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": volumes,
        "amount": [p * v * 100.0 for p, v in zip(prices, volumes, strict=True)],
    })


# ══ 特征帧 ═══════════════════════════════════════════════


def test_feature_frame_vwap_and_pct():
    df = _bars([10.0, 10.2, 9.8])
    frame = build_feature_frame(df, prev_close={SYMBOL: 10.0})
    # vwap = 累计成交额 / (累计量(手)*100)
    assert frame["vwap"][2] == pytest.approx((10.0 + 10.2 + 9.8) / 3)
    assert frame["pct_vs_prev_close"][2] == pytest.approx(9.8 / 10.0 - 1)
    assert frame["pct_from_open"][2] == pytest.approx(9.8 / 10.0 - 1)
    assert frame["price"][2] == 9.8
    # 缺昨收 → null
    frame2 = build_feature_frame(df)
    assert frame2["pct_vs_prev_close"][2] is None


def test_feature_frame_vol_ratio_windows_and_nulls():
    # 量比: 当前N根量和 / 此前N根量和; 前 2N-1 根为 null
    prices = [10.0] * 12
    volumes = [100.0, 100.0, 100.0, 100.0, 100.0, 300.0, 100.0, 100.0, 900.0, 100.0, 100.0, 100.0]
    frame = build_feature_frame(_bars(prices, volumes))
    vr1 = frame["vol_ratio_1m_today"].to_list()
    assert vr1[0] is None                      # 无前值
    assert vr1[1] == pytest.approx(100.0 / 100.0)
    assert vr1[5] == pytest.approx(300.0 / 100.0)
    assert vr1[8] == pytest.approx(900.0 / 100.0)
    vr3 = frame["vol_ratio_3m_today"].to_list()
    assert vr3[0] is None and vr3[4] is None   # 3+3-1=5 根前不足
    # 第 6 根(idx5): [3,4,5]=100+100+300 vs [0,1,2]=300
    assert vr3[5] == pytest.approx(500.0 / 300.0)
    # 前窗口量为 0 → null(不伪装成 0)
    zero_start = build_feature_frame(_bars([10.0, 10.0, 10.0], [0.0, 0.0, 500.0]))
    assert zero_start["vol_ratio_1m_today"][2] is None


def test_feature_frame_rolling_window_never_crosses_lunch():
    # 上午最后一根的 5 分钟窗口不回看跨日; 下午重新预热
    n_am, n_pm = 8, 4
    am = _bars([10.0] * n_am, [100.0] * n_am)                       # 09:30-09:37
    pm = _bars([10.0] * n_pm, [100.0] * n_pm, start=datetime(2026, 9, 4, 13, 0))
    frame = build_feature_frame(pl.concat([am, pm]))
    vr = frame["vol_ratio_3m_today"].to_list()
    # 上午 8 根: idx5 起 3/3 窗口成立(idx5,6,7 非 null); 下午 4 根全部 null(窗口不足)
    assert vr[5] is not None and vr[7] is not None
    assert all(v is None for v in vr[n_am:n_am + n_pm])


def test_feature_frame_open_30m_and_day_extremes():
    frame = build_feature_frame(_bars([10.0 + 0.1 * i for i in range(32)]))
    o30h = frame["open_30m_high_dist"].to_list()
    assert all(v is None for v in o30h[:29])      # 开盘未满 30 分钟
    # _bars 里 high = price*1.01 → 第 30 根 close/开盘30分钟最高 = 1/1.01
    assert o30h[29] == pytest.approx(1.0 / 1.01 - 1)
    assert o30h[31] == pytest.approx(13.1 / (12.9 * 1.01) - 1)
    assert frame["day_high_dist"][5] == pytest.approx(1.0 / 1.01 - 1)
    # 累计最低 = 首根 low(10.0*0.99), close_5 = 10.5
    assert frame["day_low_dist"][5] == pytest.approx(10.5 / 9.9 - 1)


def test_feature_frame_cutoff_drops_incomplete_bar():
    df = _bars([10.0, 10.1, 10.2])
    frame = build_feature_frame(df, cutoff=datetime(2026, 9, 4, 9, 32))
    assert frame.height == 2


# ══ 校验与编译 ═══════════════════════════════════════════


def _intraday_sig(**overrides) -> dict:
    sig = {
        "id": "test_sig", "name": "测试", "kind": "entry",
        "timeframe": "intraday",
        "conditions": [{"left": "price", "op": "cross_up", "right": "field:vwap"}],
    }
    sig.update(overrides)
    return sig


def test_validate_intraday_accepts_and_rejects():
    custom_signals.validate(_intraday_sig())
    custom_signals.validate(_intraday_sig(
        conditions=[{"left": "vol_ratio_3m_today", "op": ">", "right": "3"}],
        min_bars=10,
    ))
    with pytest.raises(ValueError, match="盘中字段"):
        custom_signals.validate(_intraday_sig(conditions=[{"left": "ma5", "op": ">", "right": "1"}]))
    with pytest.raises(ValueError, match="运算符"):
        custom_signals.validate(_intraday_sig(conditions=[{"left": "price", "op": "~", "right": "1"}]))
    with pytest.raises(ValueError, match="日期偏移"):
        custom_signals.validate(_intraday_sig(
            conditions=[{"left": "price", "op": ">", "right": "1", "leftDays": 1}]
        ))
    with pytest.raises(ValueError, match="min_bars"):
        custom_signals.validate(_intraday_sig(min_bars=500))
    with pytest.raises(ValueError, match="timeframe"):
        custom_signals.validate(_intraday_sig(timeframe="weekly"))


def test_build_intraday_expressions_edge_semantics():
    # 价格下探再上穿 vwap: 上升沿恰好只在上穿那根 bar 为 true
    prices = [10.0, 10.0, 10.0, 10.0, 9.8, 9.7, 9.6, 9.6, 9.6, 10.0, 10.2, 10.3]
    frame = build_feature_frame(_bars(prices))
    exprs = custom_signals.build_intraday_expressions([_intraday_sig()])
    col = custom_signals.intraday_column_name("test_sig")
    assert col == "csgi_test_sig"
    out = custom_signals.apply_intraday_edges(frame, exprs)
    fired = [i for i, v in enumerate(out[col].to_list()) if v]
    assert len(fired) == 1
    # idx8: 9.6 < vwap; idx9: 10.0 > vwap → 上穿在第 9 根(0 起)
    assert fired[0] == 9

    # 比较条件的上升沿: 持续满足只触发一次(本序列条件自首根即为真 → 只在回升的 idx9 触发)
    sig2 = _intraday_sig(id="test_state", conditions=[{"left": "price", "op": ">", "right": "9.65"}])
    exprs2 = custom_signals.build_intraday_expressions([sig2])
    out2 = custom_signals.apply_intraday_edges(frame, exprs2)
    fired2 = [i for i, v in enumerate(out2["csgi_test_state"].to_list()) if v]
    assert fired2 == [9]

    # 振荡序列: 每次 false→true 各触发一次
    osc = build_feature_frame(_bars([10.0, 9.6, 10.0, 9.6, 10.0]))
    exprs3 = custom_signals.build_intraday_expressions([sig2])
    out3 = custom_signals.apply_intraday_edges(osc, exprs3)
    assert [i for i, v in enumerate(out3["csgi_test_state"].to_list()) if v] == [2, 4]


# ══ v1 黄金等价: 旧累计循环算法作参照 ═════════════════════


def _legacy_v1_triggers(prices: list[float], prev_close: float | None) -> dict[str, int]:
    """复刻 v1 IntradaySignalEvaluator 的判定(逐 bar 累计, 边沿触发)。"""
    cum_vol = cum_amt = 0.0
    fired: dict[str, int] = {}
    prev_price = prev_vwap = None
    for i, p in enumerate(prices):
        cum_vol += 100.0
        cum_amt += p * 100.0 * 100.0
        vwap = cum_amt / (cum_vol * 100.0)
        if i >= 1:
            if prev_price <= prev_vwap and p > vwap:
                fired.setdefault("avg_up", i)
            if prev_price >= prev_vwap and p < vwap:
                fired.setdefault("avg_down", i)
            if prev_close and prev_price <= prev_close and p > prev_close:
                fired.setdefault("zero_up", i)
            if prev_close and prev_price >= prev_close and p < prev_close:
                fired.setdefault("zero_down", i)
        prev_price, prev_vwap = p, vwap
    return fired


@pytest.mark.parametrize("prices,prev_close", [
    ([10.0, 10.0, 10.0, 10.0, 9.8, 9.7, 9.6, 9.6, 9.6, 10.0, 10.2, 10.3], 10.0),
    ([9.0, 9.1, 9.2, 9.3, 9.4, 9.3, 9.2, 9.1, 9.0, 8.9, 8.8, 8.7], 9.25),
    ([10.0, 9.9, 10.1, 9.8, 10.2, 9.7, 10.3, 9.6, 10.4, 9.5, 10.5, 9.4], 9.95),
])
def test_evaluator_matches_legacy_v1(prices, prev_close):
    """新评估器逐 bar 喂入, 触发时点必须与 v1 算法完全一致。"""
    df = _bars(prices)
    evaluator = IntradaySignalEvaluator()
    got: dict[str, int] = {}
    for t in range(1, len(prices) + 1):
        now = df["datetime"][t - 1] + timedelta(minutes=1)
        rows = evaluator.evaluate(
            df.head(t), symbols={SYMBOL}, prev_close={SYMBOL: prev_close},
            asset_type="stock", now=now,
        )
        for r in rows:
            if r.get("signal_intraday_avg_cross_up"):
                got.setdefault("avg_up", t - 1)
            if r.get("signal_intraday_avg_cross_down"):
                got.setdefault("avg_down", t - 1)
            if r.get("signal_intraday_zero_cross_up"):
                got.setdefault("zero_up", t - 1)
            if r.get("signal_intraday_zero_cross_down"):
                got.setdefault("zero_down", t - 1)
    assert got == _legacy_v1_triggers(prices, prev_close)


def test_evaluator_no_refire_without_new_bar():
    prices = [10.0, 9.0, 10.5]
    df = _bars(prices)
    evaluator = IntradaySignalEvaluator()
    # 逐根喂入: 首轮只建状态; 新 bar 出现才可能触发; 同批 bar 重跑不重复触发
    fired: list[dict] = []
    for t in range(1, len(prices) + 1):
        now = df["datetime"][t - 1] + timedelta(minutes=1)
        fired += evaluator.evaluate(
            df.head(t), symbols={SYMBOL}, prev_close={SYMBOL: 10.0}, asset_type="stock", now=now,
        )
    assert fired                       # 9.0 下穿 / 10.5 上穿均有触发
    now3 = df["datetime"][2] + timedelta(minutes=1)
    again = evaluator.evaluate(df, symbols={SYMBOL}, prev_close={SYMBOL: 10.0}, asset_type="stock", now=now3)
    assert again == []                  # 无新 bar → 不重复触发


def test_evaluator_custom_csgi_signal_and_inject():
    sig = _intraday_sig(id="my_intraday", conditions=[{"left": "price", "op": "cross_up", "right": 10.05}])
    prices = [10.0, 9.9, 9.8, 10.1, 10.2, 10.3]
    df = _bars(prices)
    evaluator = IntradaySignalEvaluator()
    fired_rows = []
    for t in range(1, len(prices) + 1):
        now = df["datetime"][t - 1] + timedelta(minutes=1)
        fired_rows += evaluator.evaluate(
            df.head(t), symbols={SYMBOL}, prev_close={}, asset_type="stock",
            now=now, signals=[sig],
        )
    # 上穿 10.05 发生在 idx3 (10.1)
    assert any(r.get("csgi_my_intraday") for r in fired_rows)
    assert len([r for r in fired_rows if r.get("csgi_my_intraday")]) == 1

    # inject 的契约是"单桶结果": 只传最后一个触发桶的行
    last_bucket = fired_rows[-1:] if fired_rows else []
    enriched = pl.DataFrame({"symbol": [SYMBOL, "999999.SZ"], "close": [10.0, 5.0]})
    injected = evaluator.inject(enriched, last_bucket)
    assert injected.height == 2                          # 单桶单行, join 不膨胀
    assert injected["csgi_my_intraday"].to_list() == [True, False]   # 未触发标的补 False
    assert set(injected.columns) >= {"signal_intraday_avg_cross_up", "signal_intraday_avg_cross_down"}


def test_uses_intraday_signals_matches_csgi_fields():
    assert uses_intraday_signals({"conditions": [{"op": "truth", "field": "signal_intraday_avg_cross_up"}]})
    assert uses_intraday_signals({"conditions": [{"op": "truth", "field": "csgi_my_intraday"}]})
    assert not uses_intraday_signals({"conditions": [{"op": "truth", "field": "csg_daily_sig"}]})
    assert not uses_intraday_signals({"conditions": [{"op": ">", "field": "close", "value": 1}]})


# ══ 引擎注入 + 加载缓存 ═══════════════════════════════════


def _engine(tmp_path: Path):
    from app.strategy.engine import StrategyEngine
    custom_dir = tmp_path / "strategies" / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    return StrategyEngine(strategy_dirs=[custom_dir])


def test_engine_injects_csgi_columns_into_minute_frame(tmp_path: Path):
    sig = _intraday_sig(id="eng_sig", conditions=[{"left": "price", "op": "cross_up", "right": "field:vwap"}])
    sig_dir = tmp_path / "user_data" / "custom_signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    (sig_dir / "eng_sig.json").write_text(json.dumps(sig), encoding="utf-8")

    engine = _engine(tmp_path)
    assert engine._user_data_dir() == tmp_path
    prices = [10.0, 10.0, 9.0, 9.0, 10.5, 10.6]
    injected = engine._inject_intraday_signal_columns(_bars(prices))
    assert "csgi_eng_sig" in injected.columns
    fired = [i for i, v in enumerate(injected["csgi_eng_sig"].to_list()) if v]
    assert fired == [4]    # idx4 上穿 vwap
    # 定义缓存指纹失效: 改文件后引擎立刻读到新定义
    (sig_dir / "eng_sig.json").write_text(json.dumps(
        _intraday_sig(id="eng_sig", conditions=[{"left": "price", "op": "<", "right": "9.5"}]),
    ), encoding="utf-8")
    injected2 = engine._inject_intraday_signal_columns(_bars(prices))
    assert injected2["csgi_eng_sig"].to_list()[2] is True or injected2["csgi_eng_sig"].to_list()[2] == True  # noqa: E712


def test_engine_skips_injection_without_definitions(tmp_path: Path):
    engine = _engine(tmp_path)
    df = _bars([10.0, 10.1])
    out = engine._inject_intraday_signal_columns(df)
    assert out is df or out.equals(df)


# ══ 回放 API ═════════════════════════════════════════════


def _seed_repo(tmp_path: Path):
    from app.tickflow.repository import DataStore, KlineRepository

    def minute(day: str, prices: list[float]):
        part = tmp_path / "kline_minute" / f"date={day}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        _bars(prices, start=datetime.fromisoformat(f"{day}T09:30:00")).with_columns(
            pl.col("datetime").cast(pl.Datetime("us"))
        ).write_parquet(part)

    def daily(day: str, closes: dict[str, float]):
        part = tmp_path / "kline_daily" / f"date={day}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": list(closes), "close": list(closes.values())}).write_parquet(part)

    minute("2026-09-03", [10.0, 9.9, 9.8, 9.9, 10.0, 10.1])
    minute("2026-09-04", [10.0, 10.0, 9.0, 9.0, 10.5, 10.6])
    daily("2026-09-02", {SYMBOL: 9.95})
    daily("2026-09-03", {SYMBOL: 10.0})
    return KlineRepository(DataStore(tmp_path))


def test_intraday_replay_endpoint(tmp_path: Path):
    from app.api.signals import IntradayReplayRequest, intraday_replay

    repo = _seed_repo(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    sig = _intraday_sig(id="replay_sig")
    sig_dir = tmp_path / "user_data" / "custom_signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    (sig_dir / "replay_sig.json").write_text(json.dumps(sig), encoding="utf-8")
    daily_sig = {
        "id": "daily_sig", "name": "日线信号", "kind": "entry",
        "conditions": [{"left": "close", "op": ">", "right": "1"}],
    }
    (sig_dir / "daily_sig.json").write_text(json.dumps(daily_sig), encoding="utf-8")

    result = intraday_replay(IntradayReplayRequest(
        signal_id="replay_sig", start_date="2026-09-03", end_date="2026-09-04",
        symbols=[SYMBOL],
    ), request)
    assert result["days_scanned"] == 2
    times = [(t["date"], t["time"]) for t in result["triggers"]]
    # 09-03: 10.0→9.x→10.0 上穿在 idx4(09:34); 09-04: 上穿在 idx4(09:34)
    assert times == [("2026-09-03", "09:34:00"), ("2026-09-04", "09:34:00")]

    # 日线信号不可回放
    with pytest.raises(Exception, match="盘中"):
        intraday_replay(IntradayReplayRequest(
            signal_id="daily_sig", start_date="2026-09-03", end_date="2026-09-04",
            symbols=[SYMBOL],
        ), request)
