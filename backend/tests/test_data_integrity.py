"""数据完整性检测 (停机缺口/盘中快照) 单元测试。

判据核心: quote_ts 仅实时 flush 写入真实毫秒时间戳 (batch 拉取/盘后计算为
null); 历史交易日的 quote_ts 时刻 < 15:00 即盘中快照 → 坏。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.data_integrity import (
    AUTO_REPAIR_MAX_LAG_DAYS,
    IntegrityIssue,
    _is_snapshot,
    _quote_ts_max_ms,
    earliest_issue_day,
    prune_enriched_partitions,
    scan_recent_integrity,
    within_auto_repair_window,
)

# 2026-08-19(周三) ~ 2026-08-21(周五) 是工作日; TODAY 取 2026-08-24(周一)
TODAY = date(2026, 8, 24)
FRIDAY = date(2026, 8, 21)
THURSDAY = date(2026, 8, 20)


def _ts_ms(day: date, t: time) -> int:
    return int(datetime.combine(day, t, tzinfo=CN_TZ).timestamp() * 1000)


def _write_daily_partition(root, table: str, day: date, quote_ts: int | None, symbols=("600001.SH",)) -> None:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    n = len(symbols)
    pl.DataFrame({
        "symbol": list(symbols),
        "date": [day] * n,
        "open": [10.0] * n,
        "high": [10.1] * n,
        "low": [9.9] * n,
        "close": [10.0] * n,
        "volume": [100.0] * n,
        "amount": [1000.0] * n,
        "quote_ts": [quote_ts] * n,
    }).write_parquet(part / "part.parquet")


# ── 判据 ────────────────────────────────────────────────────────────


def test_snapshot_predicate():
    noon = _ts_ms(FRIDAY, time(11, 58))
    after_close = _ts_ms(FRIDAY, time(15, 0, 30))
    assert _is_snapshot(FRIDAY, noon) is True
    assert _is_snapshot(FRIDAY, after_close) is False
    assert _is_snapshot(FRIDAY, None) is False  # batch 历史 → 权威


def test_quote_ts_max_reads_partition_statistics(tmp_path):
    part = tmp_path / "date=2026-08-21"
    part.mkdir()
    pl.DataFrame({
        "symbol": ["a", "b"],
        "quote_ts": [1000, 2000],
    }).write_parquet(part / "part.parquet")
    assert _quote_ts_max_ms(part) == 2000


def test_quote_ts_max_none_for_all_null(tmp_path):
    part = tmp_path / "date=2026-08-21"
    part.mkdir()
    pl.DataFrame({
        "symbol": ["a", "b"],
        "quote_ts": [None, None],
    }).write_parquet(part / "part.parquet")
    assert _quote_ts_max_ms(part) is None


# ── 扫描 ────────────────────────────────────────────────────────────


def test_batch_history_with_null_quote_ts_is_clean(tmp_path):
    _write_daily_partition(tmp_path, "kline_daily", THURSDAY, None)
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    # 周六/周日非工作日不扫; 无今日分区且周五为最新 → 周五之后无缺口
    assert issues == []


def test_midday_snapshot_partition_is_flagged(tmp_path):
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(11, 58)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (FRIDAY, "kline_daily", "snapshot"),
    ]


def test_final_snapshot_after_close_is_clean(tmp_path):
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(15, 1)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_today_partition_is_never_flagged(tmp_path):
    # 今天的盘中 quote_ts 属正常实时更新
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(9, 45)))
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_missing_tail_day_flagged(tmp_path):
    # 周四有数据, 周五(工作日)整天停机缺失, 今天周一启动
    _write_daily_partition(tmp_path, "kline_daily", THURSDAY, None)
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (FRIDAY, "kline_daily", "missing"),
    ]


def test_snapshot_and_missing_both_reported(tmp_path):
    # 周四盘中快照 + 周五缺失
    _write_daily_partition(tmp_path, "kline_daily", THURSDAY, _ts_ms(THURSDAY, time(13, 30)))
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    kinds = {(i.day, i.kind) for i in issues}
    assert (THURSDAY, "snapshot") in kinds
    assert (FRIDAY, "missing") in kinds
    assert earliest_issue_day(issues) == THURSDAY


def test_no_recent_activity_not_flagged(tmp_path):
    # 最新分区早于扫描窗口 → 整族跳过 (首次启动/长期停用不自动修复)
    old = TODAY - timedelta(days=30)
    _write_daily_partition(tmp_path, "kline_daily", old, None)
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_interior_history_hole_not_flagged(tmp_path):
    # 历史内部空洞是 laggards 另一类问题, 只报"晚于本地最新分区"的尾部缺口
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert issues == []


def test_etf_family_independent(tmp_path):
    # ETF 族近期无活动 → 不判定, 即便股票族有问题
    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(11, 58)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    assert all(i.table == "kline_daily" for i in issues)
    assert earliest_issue_day(issues, ("kline_etf_daily",)) is None


def test_auto_repair_window():
    assert within_auto_repair_window(TODAY - timedelta(days=AUTO_REPAIR_MAX_LAG_DAYS), today=TODAY) is True
    assert within_auto_repair_window(TODAY - timedelta(days=AUTO_REPAIR_MAX_LAG_DAYS + 1), today=TODAY) is False
    assert within_auto_repair_window(None, today=TODAY) is False


# ── enriched 分区删除 ───────────────────────────────────────────────


def test_prune_enriched_partitions_removes_only_range(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    for day in (THURSDAY, FRIDAY, TODAY):
        part = base / f"date={day.isoformat()}"
        part.mkdir(parents=True)
        (part / "part.parquet").write_bytes(b"x")
    removed = prune_enriched_partitions(tmp_path, FRIDAY)
    assert removed == 2
    assert (base / f"date={THURSDAY.isoformat()}").exists()
    assert not (base / f"date={FRIDAY.isoformat()}").exists()
    assert not (base / f"date={TODAY.isoformat()}").exists()


# ── 管道起点决策 (分支3降级后的起点) ────────────────────────────────


def _resolve_daily_sync_start(latest_daily, stale_day):
    # 与 daily_pipeline.run_now 分支4的起点表达式一致 (min(非空值))
    return min(d for d in (latest_daily, stale_day) if d is not None)


def test_branch4_start_takes_earliest_bad_day():
    # today_exists 场景: latest=今天, 坏日=上周五 → 起点必须是上周五
    assert _resolve_daily_sync_start(TODAY, FRIDAY) == FRIDAY


def test_branch4_start_without_stale_day_uses_latest():
    assert _resolve_daily_sync_start(FRIDAY, None) == FRIDAY
    assert _resolve_daily_sync_start(TODAY, None) == TODAY


def test_timezone_conversion_is_cn():
    # quote_ts 是毫秒 Unix 时间戳, 必须按 UTC+8 折算 — 15:00 边界用例
    ts = int(datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 北京 15:00
    assert _is_snapshot(FRIDAY, ts) is False
    ts_morning = int(datetime(2026, 8, 21, 3, 58, tzinfo=timezone.utc).timestamp() * 1000)  # 北京 11:58
    assert _is_snapshot(FRIDAY, ts_morning) is True


def test_issue_from_other_day_timestamp_not_flagged(tmp_path):
    # 防御: quote_ts 日期与分区日期不符(跨天写入的脏数据)不判快照
    part = tmp_path / "kline_daily" / f"date={FRIDAY.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["a"], "date": [FRIDAY], "quote_ts": [_ts_ms(THURSDAY, time(11, 0))],
    }).write_parquet(part / "part.parquet")
    issues = scan_recent_integrity(tmp_path, today=TODAY)
    # 周五分区带周四时间戳 → 不判快照; 周四分区缺失且晚于最新(周五) → 不报
    assert issues == []


def test_describe_and_issue_dataclass():
    issues = [IntegrityIssue(day=FRIDAY, table="kline_daily", kind="snapshot")]
    from app.services.data_integrity import describe_issues

    assert "2026-08-21" in describe_issues(issues)
    assert "盘中快照" in describe_issues(issues)


# ── 开实时行情门禁 (钩子2) ──────────────────────────────────────────


def _gate_state(tmp_path, quote_service, repo):
    from types import SimpleNamespace

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            quote_service=quote_service,
            depth_service=None,
            repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            capabilities=None,
        ))
    )


class _QuoteServiceStub:
    def __init__(self, mode="market"):
        self._mode = mode
        self.enabled = False

    @staticmethod
    def is_realtime_allowed():
        return True

    @staticmethod
    def is_paused():
        return False

    @staticmethod
    def realtime_mode():
        return "market"

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False


def test_realtime_gate_blocks_on_snapshot_and_launches_repair(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api import settings as settings_api
    from app.services import data_integrity

    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, _ts_ms(FRIDAY, time(11, 58)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))

    launched = []
    monkeypatch.setattr(
        data_integrity, "launch_integrity_repair",
        lambda state, day, reason: (launched.append((day, reason)) or ("job-x", True)),
    )
    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )

    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    with pytest.raises(HTTPException) as exc_info:
        settings_api.update_realtime_quotes(req, request)

    assert exc_info.value.status_code == 409
    assert "盘中快照" in exc_info.value.detail
    assert "job-x" in exc_info.value.detail
    # 修复任务以最早坏日为起点, 且实时行情未被开启
    assert launched == [(FRIDAY, "realtime_gate")]
    assert saved == {}


def test_realtime_gate_allows_clean_data(tmp_path, monkeypatch):
    from app.api import settings as settings_api

    _write_daily_partition(tmp_path, "kline_daily", FRIDAY, None)
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))

    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )
    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    result = settings_api.update_realtime_quotes(req, request)
    assert result["realtime_quotes_enabled"] is True
    assert qs.enabled is True
    assert saved == {"realtime_quotes_enabled": True}


def test_realtime_gate_ignores_old_issues_beyond_window(tmp_path, monkeypatch):
    from app.api import settings as settings_api

    old_day = TODAY - timedelta(days=AUTO_REPAIR_MAX_LAG_DAYS + 1)
    while old_day.weekday() >= 5:
        old_day -= timedelta(days=1)
    _write_daily_partition(tmp_path, "kline_daily", old_day, _ts_ms(old_day, time(11, 58)))
    _write_daily_partition(tmp_path, "kline_daily", TODAY, _ts_ms(TODAY, time(10, 0)))

    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.save", lambda payload: saved.update(payload),
    )
    qs = _QuoteServiceStub()
    request = _gate_state(tmp_path, qs, repo=None)
    req = settings_api.RealtimeQuotesPrefs(realtime_quotes_enabled=True)

    result = settings_api.update_realtime_quotes(req, request)
    assert result["realtime_quotes_enabled"] is True


def test_boot_check_launches_repair_within_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services import data_integrity

    # boot_integrity_check 用真实"今天" — 往回找最近工作日造盘中快照分区
    launched = []
    monkeypatch.setattr(
        data_integrity, "launch_integrity_repair",
        lambda state, day, reason: (launched.append(day) or ("job-x", True)),
    )

    real_today = datetime.now(CN_TZ).date()
    probe = real_today - timedelta(days=1)
    while probe.weekday() >= 5:
        probe -= timedelta(days=1)
    data_dir = tmp_path / "boot"
    _write_daily_partition(data_dir, "kline_daily", probe, _ts_ms(probe, time(11, 58)))
    _write_daily_partition(data_dir, "kline_daily", real_today, _ts_ms(real_today, time(10, 0)))

    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)),
    )
    data_integrity.boot_integrity_check(state)
    assert launched == [probe]


# ── 管道自愈端到端 (钩子3, 离线集成) ────────────────────────────────


def _write_full_partition(root, table: str, day: date, quote_ts: int | None) -> None:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "date": [day, day],
        "open": [10.0, 20.0], "high": [10.1, 20.1],
        "low": [9.9, 19.9], "close": [10.0, 20.0],
        "volume": [100.0, 200.0], "amount": [1000.0, 4000.0],
        "quote_ts": [quote_ts, quote_ts],
    }).write_parquet(part / "part.parquet")


def test_pipeline_self_heals_snapshot_day(tmp_path, monkeypatch):
    """用户 bug 场景复刻: 昨天盘中快照 + 今天实时分区 → 管道应放弃"只刷今天",
    降级为从坏日起的范围拉取, 并把坏 enriched 分区删后重算。"""
    from app.config import settings as app_settings
    from app.jobs import daily_pipeline
    from app.services import instrument_sync, kline_sync
    from app.tickflow.repository import DataStore, KlineRepository

    today = datetime.now(CN_TZ).date()
    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)

    _write_full_partition(tmp_path, "kline_daily", yesterday, _ts_ms(yesterday, time(11, 58)))
    _write_full_partition(tmp_path, "kline_daily", today, _ts_ms(today, time(10, 0)))
    _write_full_partition(tmp_path, "kline_daily_enriched", yesterday, _ts_ms(yesterday, time(11, 58)))
    _write_full_partition(tmp_path, "kline_daily_enriched", today, _ts_ms(today, time(10, 0)))

    # 网络函数离线化: 维表同步 + 日K batch 拉取(记录参数)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    batch_calls: list[dict] = []

    def _fake_batch(universe, repo, capset, start_date=None, end_date=None, on_chunk_done=None):
        batch_calls.append({
            "start": start_date.date() if hasattr(start_date, "date") else start_date,
            "end": end_date.date() if hasattr(end_date, "date") else end_date,
        })
        return 0

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", _fake_batch)
    # run_pipeline() 不传 data_dir 时读 settings.data_dir — 同步指到 tmp
    monkeypatch.setattr(app_settings, "data_dir", tmp_path)

    repo = KlineRepository(DataStore(tmp_path))
    capset = SimpleNamespace(has=lambda key: key == "QUOTE_POOL")

    result = daily_pipeline.run_now(repo, capset)  # type: ignore[arg-type]

    # 分支3(实时覆写只刷今天)被降级 → 范围拉取起点=坏日
    assert batch_calls and batch_calls[0]["start"] == yesterday
    assert result["integrity_repair_from"] == yesterday.isoformat()
    assert result["integrity_issues"] >= 1
    # 坏 enriched 分区被删后当"新日期"重算写回 (无 prune 时 Step 2 走 skip 不写)
    enriched_left = sorted(
        p.name for p in (tmp_path / "kline_daily_enriched").glob("date=*")
    )
    assert enriched_left == [f"date={yesterday.isoformat()}", f"date={today.isoformat()}"]
    assert result["enriched_days"] > 0
