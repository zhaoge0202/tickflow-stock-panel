"""轮询放量监控 (volume_delta) 测试: 引擎命中/冷却/批量合并 + 基础过滤 + 快照差值边界。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


def _df(rows: list[dict]) -> pl.DataFrame:
    """rows 每项: symbol/_volume_delta 必填, 其余可选 (close/amount/total_shares/float_shares)。"""
    base = {
        "symbol": [], "close": [], "change_pct": [],
        "_volume_delta": [], "_volume_delta_amount": [], "_volume_delta_span": [],
    }
    optional = ["amount", "total_shares", "float_shares"]
    for r in rows:
        base["symbol"].append(r["symbol"])
        base["close"].append(r.get("close", 10.0))
        base["change_pct"].append(0.01)
        base["_volume_delta"].append(r["_volume_delta"])
        base["_volume_delta_amount"].append(r.get("_volume_delta_amount", r["_volume_delta"] * 1000.0))
        base["_volume_delta_span"].append(6.0)
    data = {k: v for k, v in base.items()}
    for col in optional:
        vals = [r.get(col) for r in rows]
        if any(v is not None for v in vals):
            data[col] = [v if v is not None else 0.0 for v in vals]
    return pl.DataFrame(data)


def _rule(**kw):
    r = {
        "id": "vd1", "name": "轮询放量", "type": "volume_delta",
        "asset_type": "stock", "scope": "all", "enabled": True,
        "threshold_volume": 9000, "cooldown_seconds": 300,
        "severity": "warn",
    }
    r.update(kw)
    return r


def test_volume_delta_hits_above_threshold():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    events = eng.evaluate(_df([
        {"symbol": "S1.SH", "_volume_delta": 9500.0, "close": 10.0},
        {"symbol": "S2.SH", "_volume_delta": 8999.0, "close": 20.0},
    ]))
    assert [e["symbol"] for e in events] == ["S1.SH"]
    ev = events[0]
    assert ev["source"] == "volume_delta"
    assert "9,500" in ev["message"] and "9,000" in ev["message"] and "间隔 6s" in ev["message"]
    assert ev["volume_delta"] == 9500.0


def test_volume_delta_no_column_degrades_silently():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    plain = pl.DataFrame({"symbol": ["S1.SH"], "close": [10.0]})
    assert eng.evaluate(plain) == []


def test_volume_delta_cooldown_suppresses_repeat():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(cooldown=300)])
    df = _df([{"symbol": "S1.SH", "_volume_delta": 12000.0}])
    assert len(eng.evaluate(df)) == 1
    assert eng.evaluate(df) == []


def test_volume_delta_batch_merge_over_five():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule()])
    rows = [{"symbol": f"S{i}.SH", "_volume_delta": 20000.0 + i} for i in range(8)]
    events = eng.evaluate(_df(rows))
    assert len(events) == 1
    assert events[0]["symbol"] == ""
    assert "共 8 只" in events[0]["message"]


def test_volume_delta_scope_filters():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(scope="symbols", symbols=["S2.SH"])])
    events = eng.evaluate(_df([
        {"symbol": "S1.SH", "_volume_delta": 9500.0},
        {"symbol": "S2.SH", "_volume_delta": 9500.0},
    ]))
    assert [e["symbol"] for e in events] == ["S2.SH"]


def test_volume_delta_metric_amount():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(metric="amount", threshold_amount=5e6)])
    events = eng.evaluate(_df([
        {"symbol": "S1.SH", "_volume_delta": 100.0, "_volume_delta_amount": 6e6},
        {"symbol": "S2.SH", "_volume_delta": 20000.0, "_volume_delta_amount": 4.9e6},
    ]))
    assert [e["symbol"] for e in events] == ["S1.SH"]
    assert "万元" in events[0]["message"]


def test_volume_delta_basic_filter_price_and_amount():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(basic_filter={
        "price_min": 5, "price_max": 100, "amount_min": 1e8, "exclude_st": False,
    })])
    events = eng.evaluate(_df([
        # 价低被滤
        {"symbol": "LOW.SH", "_volume_delta": 20000.0, "close": 3.0, "amount": 5e8},
        # 价过高被滤
        {"symbol": "HIGH.SH", "_volume_delta": 20000.0, "close": 200.0, "amount": 5e8},
        # 成交额不足被滤
        {"symbol": "THIN.SH", "_volume_delta": 20000.0, "close": 10.0, "amount": 5e7},
        # 通过
        {"symbol": "OK.SH", "_volume_delta": 20000.0, "close": 10.0, "amount": 5e8},
    ]))
    assert [e["symbol"] for e in events] == ["OK.SH"]


def test_volume_delta_basic_filter_market_cap():
    eng = MonitorRuleEngine()
    eng.set_rules([_rule(basic_filter={
        "market_cap_min": 20e8, "price_min": None, "price_max": None,
        "amount_min": None, "exclude_st": False,
    })])
    # close × total_shares: BIG 10×3e8=30亿 通过; SMALL 10×1e8=10亿 被滤
    events = eng.evaluate(_df([
        {"symbol": "BIG.SH", "_volume_delta": 20000.0, "total_shares": 3e8},
        {"symbol": "SMALL.SH", "_volume_delta": 20000.0, "total_shares": 1e8},
    ]))
    assert [e["symbol"] for e in events] == ["BIG.SH"]


def test_volume_delta_basic_filter_exclude_st():
    eng = MonitorRuleEngine()
    eng.set_name_map({"STOCK.SH": "平安银行", "STK.SH": "ST 某某"})
    eng.set_rules([_rule(basic_filter={
        "price_min": None, "price_max": None, "amount_min": None, "exclude_st": True,
    })])
    events = eng.evaluate(_df([
        {"symbol": "STOCK.SH", "_volume_delta": 20000.0},
        {"symbol": "STK.SH", "_volume_delta": 20000.0},
    ]))
    assert [e["symbol"] for e in events] == ["STOCK.SH"]


def test_validate_and_normalize_defaults():
    r = monitor_rules.normalize({"id": "vd2", "type": "volume_delta"})
    assert r["threshold_volume"] == 9000
    assert r["scope"] == "all"
    assert r["cooldown_seconds"] == 300
    assert r["metric"] == "volume"
    assert r["basic_filter"]["price_min"] == 3
    assert r["basic_filter"]["exclude_st"] is True
    # 用户字段覆盖默认
    r2 = monitor_rules.normalize({"id": "vd5", "type": "volume_delta", "basic_filter": {"price_min": 1, "exclude_st": False}})
    assert r2["basic_filter"]["price_min"] == 1
    assert r2["basic_filter"]["exclude_st"] is False
    assert r2["basic_filter"]["price_max"] == 300  # 未覆盖项保留默认
    monitor_rules.validate({"id": "vd2", "name": "n", "type": "volume_delta", "threshold_volume": 1})
    with pytest.raises(ValueError):
        monitor_rules.validate({"id": "vd3", "name": "n", "type": "volume_delta", "threshold_volume": 0})
    with pytest.raises(ValueError):
        monitor_rules.validate({"id": "vd4", "name": "n", "type": "volume_delta", "asset_type": "etf"})
    with pytest.raises(ValueError):
        monitor_rules.validate({"id": "vd6", "name": "n", "type": "volume_delta",
                                "metric": "amount", "threshold_amount": 0})
    with pytest.raises(ValueError):
        monitor_rules.validate({"id": "vd7", "name": "n", "type": "volume_delta",
                                "basic_filter": {"price_min": -1}})
    with pytest.raises(ValueError):
        monitor_rules.validate({"id": "vd8", "name": "n", "type": "volume_delta",
                                "basic_filter": {"unknown_field": 1}})


# ── 快照差值状态 (QuoteService) ──────────────────────────

def _qs(monkeypatch, *, continuous=True):
    from app.services.quote_service import QuoteService
    qs = QuoteService.__new__(QuoteService)
    qs._prev_stock_volume = None
    qs._prev_volume_fetched_at = None
    qs._prev_volume_date = None
    qs._volume_delta = {}
    qs._volume_delta_span_s = 0.0
    monkeypatch.setattr(QuoteService, "_is_continuous_trading", lambda self: continuous)
    monkeypatch.setattr(
        QuoteService, "_continuous_session_start_ms",
        staticmethod(lambda: 0.0),
    )
    monkeypatch.setattr("app.services.quote_service.cn_today", lambda: date(2026, 8, 25))
    return qs


def test_delta_computed_and_prev_updated(monkeypatch):
    qs = _qs(monkeypatch)
    t0 = 1_000_000.0
    qs._update_volume_delta(
        [{"symbol": "S1.SH", "volume": 10000, "amount": 5e6},
         {"symbol": "S2.SH", "volume": 500, "amount": 1e6}], t0,
    )
    assert qs._volume_delta == {}  # 首轮无 prev
    qs._update_volume_delta(
        [{"symbol": "S1.SH", "volume": 19500, "amount": 9.5e6},
         {"symbol": "S2.SH", "volume": 400, "amount": 2e6}], t0 + 6000,
    )
    # S2 volume cur < prev (重置) → 丢弃; S1 差值 (9500 手, 450 万元)
    assert qs._volume_delta == {"S1.SH": (9500.0, 4.5e6)}
    assert qs._volume_delta_span_s == 6.0


def test_delta_cross_day_reset(monkeypatch):
    import app.services.quote_service as qsm
    qs = _qs(monkeypatch)
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 10000}], 1000.0)
    assert qs._prev_volume_date == date(2026, 8, 25)
    monkeypatch.setattr(qsm, "cn_today", lambda: date(2026, 8, 26))
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 20000}], 2000.0)
    assert qs._volume_delta == {}
    assert qs._prev_volume_date == date(2026, 8, 26)


def test_delta_open_protection(monkeypatch):
    qs = _qs(monkeypatch)
    # 9:29 的 prev (早于 9:30 时段起点) → 9:31 本轮不触发
    session_start = 1_000_000.0
    monkeypatch.setattr(
        type(qs), "_continuous_session_start_ms",
        staticmethod(lambda: session_start),
    )
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 10000}], session_start - 60_000)
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 99999}], session_start + 60_000)
    assert qs._volume_delta == {}
    # 之后一轮 prev 已在时段内 → 恢复计算
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 109999}], session_start + 66_000)
    assert qs._volume_delta == {"S1.SH": (10000.0, 0.0)}


def test_delta_not_continuous_trading(monkeypatch):
    qs = _qs(monkeypatch, continuous=False)
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 10000}], 1000.0)
    qs._update_volume_delta([{"symbol": "S1.SH", "volume": 99999}], 7000.0)
    # 非连续竞价 (如午休) 不产差值, 但 prev 持续更新
    assert qs._volume_delta == {}
    assert qs._prev_stock_volume == {"S1.SH": (99999.0, 0.0)}


def test_inject_volume_delta_join():
    from app.services.quote_service import QuoteService
    qs = QuoteService.__new__(QuoteService)
    qs._volume_delta = {"S1.SH": (900.0, 9e5), "S9.SH": (500.0, 5e5)}
    qs._volume_delta_span_s = 6.0
    base = pl.DataFrame({"symbol": ["S1.SH", "S2.SH"], "close": [10.0, 20.0]})
    out = qs._inject_volume_delta(base)
    assert out.filter(pl.col("symbol") == "S1.SH")["_volume_delta"][0] == 900.0
    assert out.filter(pl.col("symbol") == "S1.SH")["_volume_delta_amount"][0] == 9e5
    # 未命中股票为 null (不触发)
    assert out.filter(pl.col("symbol") == "S2.SH")["_volume_delta"][0] is None
    # 空差值原样返回
    qs._volume_delta = {}
    assert qs._inject_volume_delta(base).columns == ["symbol", "close"]


def test_session_start_ms_matches_clock():
    from app.services.quote_service import QuoteService
    from datetime import datetime, time as dt_time, timedelta, timezone

    now = QuoteService._continuous_session_start_ms() / 1000.0
    start_dt = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=8)))
    assert start_dt.time() in (dt_time(9, 30), dt_time(13, 0))
