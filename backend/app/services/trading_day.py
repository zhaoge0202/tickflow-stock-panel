"""交易日探针 (oracle) — 回答「今天是否 A 股交易日」。

消费方 (实时行情轮询 / 盘中分钟增量) 在周几+时段门控之后调用, 用于把
「工作日但休市」的节假日从轮询窗口里剔除; 返回 None (未知) 时调用方
维持现状行为 (周几近似 + 快照新鲜度判据兜底), 不引入新依赖。

探测链 (按确定性排序, 先到先得):
  1. fuyao 交易日历 (已配置 fuyao 时): GET /api/a-share/calendar/trading-days,
     今天在近一年交易日列表内 ⇔ 交易日。权威日历, 无时段依赖, 无开盘缓冲问题。
  2. tdx-api 本地 sidecar 行情时间戳 (默认实时源, 不依赖外网): 拉一篮流动性票
     快照 (单请求), max(ServerTime) 日期 == 今天 ⇔ 交易日。
  3. tickflow 云端行情时间戳: 语义同 2, 但走 free-api 且免费档常无 batch 权限,
     实测每 5 分钟一次 403 且偶发数十秒超时 → 只作最后兜底, 并带超时+熔断。
     非交易日全市场戳停在上一交易日 (2026-08-29 周六实测 5551/5551, 含停牌股 —
     戳是快照定版时刻, 非最后成交时刻); 交易日集合竞价阶段 (9:15-9:30) 戳是否
     已翻新未实测 → 开盘缓冲窗内戳过期不作数, 保守视为未知。
  4. 均不可用 → None: 调用方按周几近似继续。

  「快照戳」子链 (2/3) 共用 _verdict_from_stamps 判定, 语义完全一致。

安全约束:
  - 周末直接返回 False (周几判断零成本, 不打任何请求)。
  - 探针是纯读: 只产出一个布尔判定, 不落盘、不进行情管道、不碰归属链路。
  - 只用于「降档」(休市不轮询); 休市结论 TTL 较短 (30 分钟) 定期复探,
    探针误判最坏损失一段快照且可自愈; 未知结论短 TTL (5 分钟) 防止
    轮询循环每拍重打失败的探测。
  - **探针不得阻塞调用方**: 调用方是行情轮询热路径, 而 SDK 默认超时 30s×3。
    每个网络探针都在守护线程里跑, 到 _PROBE_DEADLINE_S 即放弃结果 (判未知);
    云端 tickflow 一次 403 (免费档无 batch 权限) 即熔断到当天结束, 不再复探。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any

from app.market_time import CN_TZ, cn_now
from app.services.shared_work import SharedWork, WorkBusyError

logger = logging.getLogger(__name__)

# 戳探针的开盘缓冲窗: 此时刻之前戳仍是上一交易日属正常 (集合竞价),
# 不据此判休市。周一实测竞价戳翻新时机后可收紧。仅上午首个窗口需要。
_STALE_BUFFER_UNTIL = dt_time(9, 40)

# 单个网络探针的硬截止 (秒)。行情轮热路径等不起 SDK 的 30s×3 默认超时。
_PROBE_DEADLINE_S = 2.0

# 一篮流动性票: 探 max(timestamp), 任一戳为今日即交易日 (OR 语义)。
# 大盘蓝筹同日全部停牌 = 市场性事件, 与休市同处理无碍。
_BASKET = ("000001.SZ", "600519.SH", "600036.SH", "601318.SH", "000651.SZ")

_TTL_TRADING_S = 3600.0   # 交易日结论每小时复探 (跨日天然失效)
_TTL_HOLIDAY_S = 1800.0   # 休市结论 30 分钟复探, 误判自愈上限
_TTL_UNKNOWN_S = 300.0    # 未知结论 5 分钟后重试探测

_CACHE_LOCK = threading.Lock()
_CACHE_EPOCH = 0
# Followers degrade to the documented "unknown" state quickly while a slow
# oracle finishes; they must not occupy the whole HTTP thread pool or probe again.
_PROBES = SharedWork("market.calendar", max_pending=8, wait_timeout_s=0.25)


@dataclass
class _Cache:
    day: object | None = None
    verdict: bool | None = None
    probed_at: float = 0.0


_CACHE = _Cache()

# 云端 tickflow 探针熔断: 403 = 免费档无 batch 权限, 重试只会每 5 分钟再卡一次。
# 熔断到当天结束 (monotonic 截止点, 0 表示未熔断)。
_TF_DISABLED_UNTIL = 0.0


def cached_verdict(now: datetime | None = None) -> bool | None:
    """Read the current day's verdict without starting network work in API readers."""
    now = now or cn_now()
    if now.weekday() >= 5:
        return False
    with _CACHE_LOCK:
        if _CACHE.day == now.date() and time.monotonic() - _CACHE.probed_at < _ttl_of(_CACHE.verdict):
            return _CACHE.verdict
    return None


def reset_cache() -> None:
    """清空探针缓存与熔断状态 (测试用)。"""
    global _CACHE_EPOCH, _TF_DISABLED_UNTIL
    with _CACHE_LOCK:
        _CACHE_EPOCH += 1
        _CACHE.day = None
        _CACHE.verdict = None
        _CACHE.probed_at = 0.0
    _TF_DISABLED_UNTIL = 0.0


def _run_with_deadline(fn: Callable[[], Any], seconds: float) -> tuple[Any, bool]:
    """在守护线程里跑可能阻塞的网络探测, 到点即返回 (None, False)。

    调用方是行情轮热路径, 不能被单个探测拖住; 超时后放弃结果, 线程自行退出。
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — 探测异常一律按未知处理
            box["error"] = exc

    thread = threading.Thread(target=_target, name="trading-day-probe", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return None, False
    if "error" in box:
        logger.debug("交易日探针异常 (按未知处理): %s", box["error"])
    return box.get("value"), True


def _verdict_from_stamps(stamps: list[Any], now: datetime) -> bool | None:
    """快照时间戳 (ms) → 交易日判定; 两个戳探针共用同一口径。"""
    valid = [int(t) for t in stamps if isinstance(t, (int, float)) and t]
    if not valid:
        return None
    latest_day = datetime.fromtimestamp(max(valid) / 1000, tz=CN_TZ).date()
    if latest_day == now.date():
        return True
    if now.time() < _STALE_BUFFER_UNTIL:
        return None
    return False


def _probe_fuyao(now: datetime) -> bool | None:
    """fuyao 交易日历: 今天在列表内 ⇔ 交易日。未配置 fuyao / 失败 → None。"""
    try:
        from app.data_providers import custom as custom_sources

        if not custom_sources.is_custom_provider("fuyao"):
            return None
        provider = custom_sources.get_provider("fuyao")
        days = provider.trading_days()
        return now.date() in days if days else None
    except Exception:  # noqa: BLE001 — 探针失败按未知处理, 不上抛
        return None


def _probe_tdxapi(now: datetime) -> bool | None:
    """tdx-api 本地 sidecar 行情时间戳: max(ServerTime) 日期 == 今天 ⇔ 交易日。

    与 tickflow 戳探针同口径, 但走本机 sidecar (127.0.0.1:8080) —— 不依赖外网,
    也是本项目实时行情的实际来源。未配置/启动失败 → None。
    """
    def _fetch() -> list[int]:
        from app.plugins.tdxapi.provider import TDXAPIProvider

        provider = TDXAPIProvider()
        try:
            return provider.probe_quote_timestamps(list(_BASKET))
        finally:
            provider.close()

    stamps, finished = _run_with_deadline(_fetch, _PROBE_DEADLINE_S)
    if not finished or stamps is None:
        return None
    return _verdict_from_stamps(stamps, now)


def _tickflow_probe_disabled() -> bool:
    return _TF_DISABLED_UNTIL > 0.0 and time.monotonic() < _TF_DISABLED_UNTIL


def _disable_tickflow_probe(reason: str) -> None:
    """云端 tickflow 探针熔断到当天结束, 避免每 5 分钟重复阻塞行情轮。"""
    global _TF_DISABLED_UNTIL
    if _tickflow_probe_disabled():
        return
    now = cn_now()
    day_end = datetime.combine(now.date() + timedelta(days=1), dt_time(0, 0), tzinfo=CN_TZ)
    _TF_DISABLED_UNTIL = time.monotonic() + max(60.0, (day_end - now).total_seconds())
    logger.warning("交易日探针 tickflow 已熔断至当日结束: %s", reason)


def _probe_tickflow_raw(now: datetime) -> bool | None:
    """tickflow 行情时间戳: max(timestamp) 日期 == 今天 ⇔ 交易日。

    戳停在上一交易日: 开盘缓冲窗内 → None (可能是竞价未翻新), 窗后 → False。
    403 (免费档无 batch 权限) → 熔断当天, 不再复探; 其它失败 → None。
    """
    try:
        from app.tickflow.client import get_client

        rows = get_client().quotes.get(symbols=list(_BASKET)) or []
    except Exception as exc:  # noqa: BLE001 — 无权限/网络失败按未知处理
        if getattr(exc, "status_code", None) == 403:
            _disable_tickflow_probe("免费档无 batch 权限 (403)")
        return None
    return _verdict_from_stamps(
        [r.get("timestamp") for r in rows if isinstance(r, dict)], now,
    )


def _probe_tickflow(now: datetime) -> bool | None:
    """tickflow 兜底探针: 已熔断则零成本跳过, 否则带硬截止执行。"""
    if _tickflow_probe_disabled():
        return None
    verdict, finished = _run_with_deadline(lambda: _probe_tickflow_raw(now), _PROBE_DEADLINE_S)
    if not finished:
        _disable_tickflow_probe(f"探测超过 {_PROBE_DEADLINE_S:.0f}s 未返回")
    return verdict


def is_trading_day(now: datetime | None = None) -> bool | None:
    """今天是否 A 股交易日。True=交易日, False=确定休市, None=未知 (维持周几近似)。

    周末零成本直判; 工作日走探测链 (fuyao 日历 → tdx-api 戳 → tickflow 戳),
    结论按 TTL 缓存。线程安全: 实时行情与分钟增量两个线程共用。
    """
    now = now or cn_now()
    if now.weekday() >= 5:
        return False

    with _CACHE_LOCK:
        epoch = _CACHE_EPOCH
        # 「未知」(None) 也是一个结论, 同样按 TTL 缓存 —— 它正是 _TTL_UNKNOWN_S 要
        # 挡住的场景 (未配 fuyao 且 tickflow 不可用时, 轮询每拍都会重打一次探测)。
        # _CACHE.day 只在探测写回时设置, 因此「当天已探过」用它判定即可。
        if (
            _CACHE.day == now.date()
            and (time.monotonic() - _CACHE.probed_at) < _ttl_of(_CACHE.verdict)
        ):
            return _CACHE.verdict

    def probe():
        # A caller can reach single-flight just after another probe retired.
        with _CACHE_LOCK:
            if (_CACHE.day == now.date()
                    and time.monotonic() - _CACHE.probed_at < _ttl_of(_CACHE.verdict)):
                return _CACHE.verdict
        verdict = _probe_fuyao(now)
        if verdict is None:
            verdict = _probe_tdxapi(now)
        if verdict is None:
            verdict = _probe_tickflow(now)

        with _CACHE_LOCK:
            if epoch != _CACHE_EPOCH:
                return None
            _CACHE.day = now.date()
            _CACHE.verdict = verdict
            _CACHE.probed_at = time.monotonic()
        return verdict

    try:
        return _PROBES.run((now.date(), epoch), probe)
    except WorkBusyError:
        return None


def _ttl_of(verdict: bool | None) -> float:
    if verdict is True:
        return _TTL_TRADING_S
    if verdict is False:
        return _TTL_HOLIDAY_S
    return _TTL_UNKNOWN_S
