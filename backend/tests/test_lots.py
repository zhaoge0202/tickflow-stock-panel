"""批次 (持仓提醒) 测试: 批次→规则映射 + 校验 + sync 一致性 (锁/校验先行/级联删除)。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import lots as lots_api
from app.strategy import lots as lots_domain
from app.strategy import monitor_rules


def _lot(**overrides):
    lot = {
        "id": "lot_test1",
        "symbol": "600519.SH",
        "qty": 100,
        "cost_price": 1500.0,
        "buy_date": "2026-08-01",
        "target_pct": 10,
        "stop_pct": 5,
        "remind_date": "2026-09-01",
        "lead_days": 2,
    }
    lot.update(overrides)
    return lot


class _EngineStub:
    def __init__(self) -> None:
        self.set_calls = 0
        self.rules = []

    def set_rules(self, rules) -> None:
        self.set_calls += 1
        self.rules = rules


def _make_request(tmp_path: Path, engine: _EngineStub):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _symbol: "stock",
    )
    state = SimpleNamespace(repo=repo, monitor_engine=engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _lot_path(tmp_path: Path, lot_id: str) -> Path:
    return tmp_path / "user_data" / "lots" / f"{lot_id}.json"


def _patch_channels(monkeypatch) -> None:
    monkeypatch.setattr("app.services.preferences.get_webhook_default_channels", lambda: [])


# ── 纯映射: 批次 → 规则 ────────────────────────────────
def test_lot_to_rules_price_and_date():
    price, date_rule = lots_domain.lot_to_rules(_lot())
    assert price is not None and date_rule is not None
    assert price["id"] == "lot_test1_p" and price["type"] == "price"
    assert price["scope"] == "symbols" and price["symbols"] == ["600519.SH"]
    assert price["conditions"] == [
        {"field": "close", "op": ">=", "value": 1650.0},  # 1500*1.10
        {"field": "close", "op": "<=", "value": 1425.0},  # 1500*0.95
    ]
    assert price["logic"] == "or"
    assert price["cooldown_seconds"] == 86400
    assert price["lot_id"] == "lot_test1"
    assert "止盈10%" in price["message"] and "止损5%" in price["message"]
    assert date_rule["id"] == "lot_test1_d" and date_rule["type"] == "date"
    assert date_rule["remind_date"] == "2026-09-01" and date_rule["lead_days"] == 2


def test_lot_to_rules_optional_parts():
    # 只有止盈 (无止损/无到期) → 无 date 规则
    price, date_rule = lots_domain.lot_to_rules(_lot(stop_pct=0, remind_date=None))
    assert price is not None and date_rule is None
    assert len(price["conditions"]) == 1
    # 只有到期 (无止盈/止损) → 无 price 规则
    price, date_rule = lots_domain.lot_to_rules(_lot(target_pct=0, stop_pct=0))
    assert price is None and date_rule is not None


def test_validate_lot_rules_and_errors():
    lots_domain.validate_lot(lots_domain.normalize_lot(_lot()))
    for overrides in (
        {"symbol": " "},
        {"cost_price": 0},
        {"qty": -1},
        {"target_pct": -1},
        {"stop_pct": -1},
        {"lead_days": -1},
        {"remind_date": "2026/09/01"},
        {"buy_date": "bad"},
        {"target_pct": 0, "stop_pct": 0, "remind_date": None},
    ):
        with pytest.raises(ValueError):
            lots_domain.validate_lot({**_lot(), **overrides})


def test_normalize_lot_defaults():
    n = lots_domain.normalize_lot({"id": "lot_x", "symbol": "600519.SH", "cost_price": 10})
    assert n["qty"] == 0 and n["lead_days"] == 1
    assert n["created_at"]
    assert n["symbol"] == "600519.SH"


# ── sync_lot 一致性 (校验先行 / 级联 / 单次重载) ─────────
def test_sync_lot_validates_rules_before_write(tmp_path, monkeypatch):
    engine = _EngineStub()
    request = _make_request(tmp_path, engine)
    _patch_channels(monkeypatch)
    reloaded = []
    monkeypatch.setattr(lots_api, "_reload_engine", lambda r: reloaded.append(1))

    def boom(_rule) -> None:
        raise ValueError("bad rule")

    monkeypatch.setattr("app.strategy.monitor_rules.validate", boom)
    with pytest.raises(HTTPException) as ei:
        lots_api.sync_lot(request, _lot())
    assert ei.value.status_code == 400
    # 批次文件、规则文件、引擎重载 都不该发生 (避免半成品)
    assert not _lot_path(tmp_path, "lot_test1").exists()
    assert reloaded == []
    rules_dir = tmp_path / "user_data" / "monitor_rules"
    assert not rules_dir.exists() or list(rules_dir.glob("*.json")) == []


def test_sync_lot_writes_lot_rules_and_reloads_once(tmp_path, monkeypatch):
    engine = _EngineStub()
    request = _make_request(tmp_path, engine)
    _patch_channels(monkeypatch)
    reloaded = []
    monkeypatch.setattr(lots_api, "_reload_engine", lambda r: reloaded.append(1))

    lots_api.sync_lot(request, _lot())
    assert _lot_path(tmp_path, "lot_test1").exists()
    price = monitor_rules.load_one(tmp_path, "lot_test1_p")
    date_rule = monitor_rules.load_one(tmp_path, "lot_test1_d")
    assert price is not None and price["lot_id"] == "lot_test1"
    assert price["conditions"][0] == {"field": "close", "op": ">=", "value": 1650.0}
    assert date_rule is not None and date_rule["remind_date"] == "2026-09-01"
    assert len(reloaded) == 1


def test_sync_lot_removes_rules_when_monitor_point_removed(tmp_path, monkeypatch):
    engine = _EngineStub()
    request = _make_request(tmp_path, engine)
    _patch_channels(monkeypatch)
    monkeypatch.setattr(lots_api, "_reload_engine", lambda r: None)
    lots_api.sync_lot(request, _lot())
    # 编辑后只剩止盈, 无到期 → date 规则应被级联删除
    lots_api.sync_lot(request, _lot(remind_date=None))
    assert monitor_rules.load_one(tmp_path, "lot_test1_d") is None
    assert monitor_rules.load_one(tmp_path, "lot_test1_p") is not None


def test_delete_lot_removes_lot_and_both_rules(tmp_path, monkeypatch):
    engine = _EngineStub()
    request = _make_request(tmp_path, engine)
    _patch_channels(monkeypatch)
    monkeypatch.setattr(lots_api, "_reload_engine", lambda r: None)
    lots_api.sync_lot(request, _lot())
    lots_api.delete_lot("lot_test1", request)
    assert not _lot_path(tmp_path, "lot_test1").exists()
    assert monitor_rules.load_one(tmp_path, "lot_test1_p") is None
    assert monitor_rules.load_one(tmp_path, "lot_test1_d") is None


def test_sync_lot_etf_resolves_asset_type(tmp_path, monkeypatch):
    engine = _EngineStub()
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _symbol: "etf",
    )
    state = SimpleNamespace(repo=repo, monitor_engine=engine)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    _patch_channels(monkeypatch)
    monkeypatch.setattr(lots_api, "_reload_engine", lambda r: None)
    lots_api.sync_lot(request, _lot(symbol="510300.SH"))
    # 止盈止损价格规则须走 ETF 监控轮才会触发, 故 asset_type 必须为 etf
    price = monitor_rules.load_one(tmp_path, "lot_test1_p")
    date_rule = monitor_rules.load_one(tmp_path, "lot_test1_d")
    assert price is not None and price["asset_type"] == "etf"
    assert date_rule is not None and date_rule["asset_type"] == "etf"


def test_upsert_lot_invalid_returns_400(tmp_path, monkeypatch):
    engine = _EngineStub()
    request = _make_request(tmp_path, engine)
    _patch_channels(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        lots_api.upsert_lot(lots_api.LotModel(**_lot(cost_price=0)), request)
    assert ei.value.status_code == 400
    assert "cost_price" in str(ei.value.detail)
