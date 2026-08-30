"""fetch_intraday_full_market_burst 单块容错契约。

一个块失败不得拖垮整轮: 成功块必须照常返回供落盘; 失败块单独重试一次;
失败块过多 (系统性故障) 时跳过重试。全部用假 client, 不发真实网络请求。
"""
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl

from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _capset(batch: int = 2) -> CapabilitySet:
    return CapabilitySet({Cap.INTRADAY_BATCH: CapabilityLimits(rpm=60, batch=batch)})


def _frame() -> dict:
    # _normalize_minute 的最小输入: 毫秒 timestamp → 北京墙钟 datetime。
    # 时间必须落在交易时段 (时区契约守卫会拒绝非交易小时的脏数据)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = int(datetime(2026, 8, 28, 9, 31, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    # as_dataframe=False 的最小 CompactKlineData (字段 → 列数组)
    return {
        "timestamp": [ts],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [100.0], "amount": [100.0],
    }


class _FakeKlines:
    """intraday_batch 假实现: fail_once 首次失败重试成功, fail_always 恒失败。"""

    def __init__(self, fail_once: set[str], fail_always: set[str]) -> None:
        self.fail_once = set(fail_once)
        self.fail_always = set(fail_always)
        self.calls: list[list[str]] = []

    def intraday_batch(self, chunk, **kwargs):
        self.calls.append(list(chunk))
        syms = set(chunk)
        if syms & self.fail_always:
            raise RuntimeError("permanent failure")
        if syms & self.fail_once:
            self.fail_once -= syms
            raise RuntimeError("transient failure")
        return {s: _frame() for s in chunk}


def _run(symbols: list[str], fake: _FakeKlines, batch: int = 2):
    client = SimpleNamespace(klines=fake)
    with patch.object(kline_sync, "get_client", return_value=client):
        return kline_sync.fetch_intraday_full_market_burst(symbols, _capset(batch))


def test_transient_chunk_failure_retried_and_all_symbols_returned():
    symbols = [f"S{i}" for i in range(6)]  # 3 chunks (batch=2)
    fake = _FakeKlines(fail_once={"S0", "S1"}, fail_always=set())
    df, requests = _run(symbols, fake)
    # 失败块 (S0,S1) 重试后成功 → 6 只全在, 请求数 = 3 块 + 1 次重试
    assert set(df["symbol"].to_list()) == set(symbols)
    assert requests == 4


def test_permanent_chunk_failure_skipped_without_losing_other_chunks():
    symbols = [f"S{i}" for i in range(6)]
    fake = _FakeKlines(fail_once=set(), fail_always={"S4", "S5"})
    df, requests = _run(symbols, fake)
    # 失败块重试仍失败 → 只跳过该块, 其余 4 只必须返回 (旧实现会整轮丢弃)
    assert set(df["symbol"].to_list()) == {"S0", "S1", "S2", "S3"}
    assert requests == 4


def test_all_chunks_succeed_requests_equals_chunk_count():
    symbols = [f"S{i}" for i in range(6)]
    fake = _FakeKlines(fail_once=set(), fail_always=set())
    df, requests = _run(symbols, fake)
    assert set(df["symbol"].to_list()) == set(symbols)
    assert requests == 3


def test_systemic_failure_skips_retry_to_avoid_pressuring_overloaded_server():
    symbols = [f"S{i}" for i in range(12)]  # 6 chunks (batch=2)
    # 5 个块恒失败 (>4) → 系统性故障, 不再重试
    fake = _FakeKlines(fail_once=set(), fail_always={s for s in symbols if s not in ("S0", "S1")})
    df, requests = _run(symbols, fake)
    assert set(df["symbol"].to_list()) == {"S0", "S1"}
    assert requests == 6  # 无重试
    assert len(fake.calls) == 6
