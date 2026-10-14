"""本地策略情报配置加载与缺失安全降级的回归测试。

生产策略 ID / 竞价阈值放在 ``data/local_config`` (gitignored), 不入 git。
本文件只用占位 ID/阈值, 验证「配置缺失 → 跳过双刃合专属分支」与「配置注入 → 分支生效」。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.services import auction_replay, focus_versions
from app.services.local_strategy_config import (
    focus_default_strategy_id,
    focus_strategy_ids,
    load_auction_config,
)


def _write_config(data_dir, **overrides):
    cfg = {
        "preselect_strategy_ids": ["p1", "p2"],
        "dynamic_strategy_ids": ["d1"],
        "focus_strategy_ids": ["f1", "f2"],
        "focus_default_strategy_id": "f1",
        "auction_gap": {
            "strategy_ids": ["f1", "g1"],
            "default_gap_max": 2.0,
            "gap_max_by_strategy": {"g1": 3.0},
            "gap_min": 1.5,
            "auction_min_change": 1.0,
        },
    }
    cfg.update(overrides)
    path = Path(data_dir) / "local_config" / "auction_strategy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def test_missing_config_returns_empty(tmp_path):
    cfg = load_auction_config(tmp_path)
    assert cfg["preselect_strategy_ids"] == []
    assert cfg["dynamic_strategy_ids"] == []
    assert cfg["focus_strategy_ids"] == []
    assert cfg["focus_default_strategy_id"] is None
    assert cfg["auction_gap"] is None
    assert focus_strategy_ids(tmp_path) == []
    assert focus_default_strategy_id(tmp_path) is None


def test_config_loaded_and_normalized(tmp_path):
    _write_config(tmp_path)
    cfg = load_auction_config(tmp_path)
    assert cfg["preselect_strategy_ids"] == ["p1", "p2"]
    assert cfg["dynamic_strategy_ids"] == ["d1"]
    assert cfg["focus_strategy_ids"] == ["f1", "f2"]
    assert cfg["focus_default_strategy_id"] == "f1"
    gap = cfg["auction_gap"]
    assert gap is not None
    assert gap["strategy_ids"] == frozenset({"f1", "g1"})
    assert gap["default_gap_max"] == 2.0
    assert gap["gap_max_by_strategy"] == {"g1": 3.0}
    assert gap["gap_min"] == 1.5
    assert gap["auction_min_change"] == 1.0


def test_malformed_config_degrades(tmp_path):
    path = Path(tmp_path) / "local_config" / "auction_strategy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_auction_config(tmp_path)["focus_default_strategy_id"] is None
    assert load_auction_config(tmp_path)["auction_gap"] is None


def test_dynamic_rejection_reason_skips_special_branch_without_config():
    reason_code, reason = auction_replay._dynamic_rejection_reason(
        "whatever",
        {"close": 9.11},
        {"last_price": 8.95},
        {"last_price": 8.95, "open": 8.95},
        {},
    )
    assert reason_code == "dynamic_strategy_rejected"
    assert "动态重算" in reason


def test_dynamic_rejection_reason_uses_injected_config(tmp_path):
    _write_config(tmp_path)
    gap_cfg = load_auction_config(tmp_path)["auction_gap"]
    reason_code, reason = auction_replay._dynamic_rejection_reason(
        "g1",
        {"close": 9.11},
        {"last_price": 8.95},
        {"last_price": 8.95, "open": 8.95},
        {},
        auction_gap_config=gap_cfg,
    )
    assert reason_code == "auction_gap_failed"
    assert "最低高开 1.5%" in reason


def test_focus_versions_degrades_without_config(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    payload = focus_versions.build_focus_three_versions(
        repo,
        None,
        as_of=date(2026, 9, 7),
    )
    assert payload["current_stage"] == "unconfigured"
    assert payload["strategy_id"] is None
    assert payload["versions"]["preview"]["total"] == 0
