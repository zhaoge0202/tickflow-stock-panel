"""`QuoteService.boot_check()` 的**启动决策 + 可观测性 + 不落盘**回归测试。

背景(本机实测事故 2026-09-16): 面板 08:58 起, 直到用户 09:35:33 手动开启才有行情
⇒ 开盘后 6 分钟实时评估全空。根因 = 旧版 `stop()` 把 `realtime_quotes_enabled` 写成 False
(lifespan shutdown 也调 `stop`) ⇒ 次日 `boot_check()` 读到 False 就**静默不启动**,
且**一行日志都不打** ⇒ 排查只能靠排除法。

在此基础上, 档位不足时 `boot_check()` 过去还会把偏好写成 False 落盘, 次日档位恢复后
依然读到 False ⇒ 又一轮静默不启动。本文件断言: 档位不足只停本次运行、留醒目日志,
**不改写 preferences.json** (mtime 与键值都不变); 偏好落盘只归 enable/disable。

⚠️ 与本文件的分工:
  * `tests/test_realtime_preference_persistence.py` 管**根因**(`stop()` 不持久化 / `disable()` 才持久化)
    —— 上游已有该文件, 本文件**不重复**它的断言。
  * 本文件管 `boot_check()` **拿这个偏好做了什么决策**、**有没有留下痕迹**
    —— 上游此前对 `boot_check()` 本身**没有任何测试**
      (`test_data_integrity.py` 里那个同名 `boot_check` 是数据修复用的, ⛔ 不是本函数)。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.services import preferences
from app.services.quote_service import QuoteService


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    """把 data_dir 隔离到 tmp_path: boot_check() 的降级写盘不得落到真实数据目录。

    `boot_check()` 在「档位不允许 + 偏好为开」时会 `preferences.save(...)` 把
    实时行情开关写 False; 不隔离时本文件会真写 `<data_dir>/user_data/preferences.json`,
    跑一次测试就把本机实时行情偏好改成关闭 (小测 #4 的 L-2)。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()


def _make(monkeypatch, *, tier_allowed: bool, pref_enabled: bool):
    """构造一个不真启动线程的 QuoteService, 并返回 (qs, started 记录器)。"""
    monkeypatch.setattr(QuoteService, "is_realtime_allowed", classmethod(lambda cls: tier_allowed))
    monkeypatch.setattr(preferences, "get_realtime_quotes_enabled", lambda: pref_enabled)
    qs = QuoteService()
    started: list = []
    monkeypatch.setattr(qs, "start", lambda *a, **k: started.append(1))
    return qs, started


def test_starts_when_preference_enabled(monkeypatch):
    """⭐ 反向对照: 偏好为开且档位允许 ⇒ **必须**启动(防止把守卫写成「永远不启动」)。"""
    qs, started = _make(monkeypatch, tier_allowed=True, pref_enabled=True)
    qs.boot_check()
    assert started == [1], "偏好为开且档位允许时必须启动"


def test_does_not_start_when_preference_disabled(monkeypatch):
    """偏好为关 ⇒ 不得启动(这正是 09-16 事故的表象; 根因已由 stop() 修掉)。"""
    qs, started = _make(monkeypatch, tier_allowed=True, pref_enabled=False)
    qs.boot_check()
    assert started == [], "偏好为关时不应启动"


def test_logs_and_does_not_start_when_tier_disallows(monkeypatch, caplog):
    """档位 none ⇒ 不启动 **且必须留日志**(09-16 事故的教训: 静默不可排查)。"""
    qs, started = _make(monkeypatch, tier_allowed=False, pref_enabled=True)
    with caplog.at_level(logging.INFO, logger="app.services.quote_service"):
        qs.boot_check()
    assert started == [], "档位 none 时不应启动"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("未启动" in m for m in msgs), f"档位不允许时必须留下可解析的日志行, 实际={msgs}"


def test_tier_disallow_does_not_persist_preference(monkeypatch, tmp_path):
    """档位 none 且偏好为开 ⇒ 只停本次运行, **不得改写 preferences.json**。

    这是 09-16 事故的二次回潮路径: 档位不足把偏好写成 False 落盘, 次日档位
    恢复后 boot_check 又读到 False 静默不启动。断言 mtime 与键值都不变。
    """
    pref_path = tmp_path / "user_data" / "preferences.json"
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    original = {"realtime_quotes_enabled": True, "watchlist_groups_in_nav": True}
    pref_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    mtime_before = pref_path.stat().st_mtime_ns

    qs, started = _make(monkeypatch, tier_allowed=False, pref_enabled=True)
    qs.boot_check()

    assert started == [], "档位 none 时不应启动"
    assert pref_path.stat().st_mtime_ns == mtime_before, "档位不足时不得改写 preferences.json"
    assert json.loads(pref_path.read_text(encoding="utf-8")) == original, "偏好键值不得变化"


def test_preference_disabled_leaves_a_log(monkeypatch, caplog):
    """偏好为关而档位允许时也应留下日志。

    「不启动」本身没有错, 错的是一句话都不说 —— 那让「没行情」与「没权限/没数据/没启动」无法区分。
    """
    qs, started = _make(monkeypatch, tier_allowed=True, pref_enabled=False)
    with caplog.at_level(logging.INFO, logger="app.services.quote_service"):
        qs.boot_check()
    assert started == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("未启动" in m for m in msgs), f"偏好为关时也应留日志, 实际={msgs}"
