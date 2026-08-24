"""监控规则 scope=watchlist_group — 自选分组动态作用域。

覆盖: 规则校验/normalize、引擎按分组当前成员过滤 (分组增删自选后无需改规则,
下一轮评估自动生效)、分组删除 fail-closed、异动规则分组过滤、API 保存时
分组存在性校验与列表 runtime_warning。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import monitor_rules as monitor_rules_api
from app.config import settings
from app.services import watchlist
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


def _group_rule(rid="r_grp", group_id="g1", **overrides):
    rule = {
        "id": rid, "name": rid, "type": "signal", "asset_type": "stock",
        "scope": "watchlist_group", "group_id": group_id, "symbols": [],
        "logic": "or",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0, "enabled": True,
    }
    rule.update(overrides)
    return rule


def _stock_df():
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ", "300750.SZ"],
        "name": ["浦发银行", "平安银行", "宁德时代"],
        "close": [10.0, 12.0, 200.0],
        "change_pct": [1.0, 2.0, 3.0],
        "rsi_14": [40.0, 50.0, 60.0],
    })


# ── 校验与 normalize ─────────────────────────────────────

def test_group_scope_validation():
    with pytest.raises(ValueError, match="自选分组"):
        monitor_rules.validate(_group_rule(group_id=None))
    with pytest.raises(ValueError, match="自选分组"):
        monitor_rules.validate(_group_rule(group_id="  "))
    with pytest.raises(ValueError, match="仅支持个股"):
        monitor_rules.validate(_group_rule(asset_type="etf"))
    # 分时穿越信号仅支持指定标的 (沿用既有限制)
    with pytest.raises(ValueError, match="分时穿越"):
        monitor_rules.validate(_group_rule(
            conditions=[{"field": "signal_intraday_avg_cross_up", "op": "truth"}],
        ))
    monitor_rules.validate(_group_rule())  # 合法


def test_normalize_group_scope_fields():
    # 分组作用域: 保留 group_id, 清掉 symbols (成员动态来自分组)
    r = monitor_rules.normalize(_group_rule(symbols=["600000.SH"]))
    assert r["group_id"] == "g1"
    assert r["symbols"] == []
    # 非分组作用域: 清掉残留 group_id
    r = monitor_rules.normalize(_group_rule(scope="all"))
    assert r["group_id"] is None


# ── 引擎: 动态成员过滤 ───────────────────────────────────

def test_engine_group_scope_dynamic_members(monkeypatch, tmp_path):
    """分组内后续加入的标的, 无需修改规则即自动进入监控范围。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("核心池")
    gid = group["id"]
    watchlist.add("600000.SH", group_id=gid)
    watchlist.add("000001.SZ", group_id=gid)

    eng = MonitorRuleEngine()
    eng.set_rules([_group_rule(group_id=gid)])
    df = _stock_df()

    events = eng.evaluate(df)
    assert {e["symbol"] for e in events} == {"600000.SH", "000001.SZ"}

    # 分组新增宁德时代 → 同一条规则下一轮自动覆盖 (版本号缓存立即失效)
    watchlist.add("300750.SZ", group_id=gid)
    events = eng.evaluate(df)
    assert "300750.SZ" in {e["symbol"] for e in events}

    # 移出分组 → 自动退出监控范围
    watchlist.remove_from_group("300750.SZ", gid)
    events = eng.evaluate(df)
    assert "300750.SZ" not in {e["symbol"] for e in events}


def test_engine_group_scope_missing_group_fail_closed(monkeypatch, tmp_path):
    """分组已删除: 不崩、不触发、绝不退化为全市场。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    watchlist.create_group("核心池")  # 让分组文件存在, 但规则绑定的 id 不在其中

    eng = MonitorRuleEngine()
    eng.set_rules([_group_rule(group_id="ghost")])
    assert eng.evaluate(_stock_df()) == []


def test_engine_group_scope_empty_group(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("空组")

    eng = MonitorRuleEngine()
    eng.set_rules([_group_rule(group_id=group["id"])])
    assert eng.evaluate(_stock_df()) == []


def test_abnormal_group_scope_filtering(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("异动池")
    gid = group["id"]
    watchlist.add("600000.SH", group_id=gid)

    def _row(symbol, dev_3d):
        return {
            "symbol": symbol, "name": symbol, "board": "主板", "st": False,
            "close": 10.0, "rt_pct": 1.0,
            "windows": {"3d": {"value": dev_3d, "threshold": 0.2, "closeness": abs(dev_3d) / 0.2}},
        }

    eng = MonitorRuleEngine()
    eng.set_rules([_group_rule(
        rid="r_ab", group_id=gid, type="abnormal",
        scope="watchlist_group", threshold_pct=70, direction="both",
        conditions=[], symbols=[],
    )])
    high_rows = [_row("600000.SH", 0.16), _row("000001.SZ", 0.18), _row("300750.SZ", 0.19)]
    low_rows = [_row("600000.SH", 0.10), _row("000001.SZ", 0.05), _row("300750.SZ", 0.05)]
    # 边缘触发: 首轮观测不告警, 回落置 False 后再次上穿才触发
    eng.evaluate_abnormal(low_rows)
    events = eng.evaluate_abnormal(high_rows)
    # 只有分组内的 600000.SH 触发; 组外两只偏离更高也不会告警
    assert [e["symbol"] for e in events] == ["600000.SH"]


# ── API: 保存校验 + 列表警告 ────────────────────────────

def _fake_request(tmp_path):
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.resolve_asset_type.return_value = "stock"
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_api_save_rejects_missing_group(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    watchlist.create_group("真实分组")

    req = _fake_request(tmp_path)
    model = monitor_rules_api.RuleModel(**_group_rule(group_id="ghost"))
    with pytest.raises(HTTPException) as exc_info:
        monitor_rules_api.save_rule(model, req)
    assert exc_info.value.status_code == 400


def test_api_save_and_list_group_rule(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("核心池")

    req = _fake_request(tmp_path)
    model = monitor_rules_api.RuleModel(**_group_rule(group_id=group["id"]))
    resp = monitor_rules_api.save_rule(model, req)
    assert resp["ok"] is True
    assert resp["rule"]["group_id"] == group["id"]

    listed = monitor_rules_api.list_rules(req)
    rule = next(r for r in listed["rules"] if r["id"] == "r_grp")
    assert "runtime_warning" not in rule

    # 分组删除后: 列表标注警告 (引擎侧同轮已 fail-closed)
    watchlist.delete_group(group["id"])
    listed = monitor_rules_api.list_rules(req)
    rule = next(r for r in listed["rules"] if r["id"] == "r_grp")
    assert "已删除" in rule["runtime_warning"]
