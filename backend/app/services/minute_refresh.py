"""盘中分钟K增量落盘服务 (Expert 专有)。

两段式拉取 (见 feat/minute-strategy 方案):
- 全天修复轮: intraday.batch (日内分时批量) 并发脉冲一次拉全市场当日全部
  分钟K — 冷启动 (如 10 点才开服务, 补 9:30 起缺口) / 覆盖滞后超阈值 /
  连续空轮自愈时触发。
- 稳态增量轮: intraday.universe 传 CN_Equity_A 标的池, 单请求返回全市场
  每只最新 3 根 (服务端上限), 靠 _write_minute_partition 的
  unique(symbol,datetime) 幂等合并滚出全天。

单轮合并写入当日 kline_minute 分区, 供分钟策略 (minute_filter) 读到新鲜数据。

设计约束:
- Expert 专有: 能力门控 Cap.INTRADAY_UNIVERSE (全量分钟) — 仅 TickFlow
  Expert 档具备, 天然排他 (自定义分钟源无此能力, 且服务本就让位插件)。
- 修复轮并发脉冲: 全市场按 batch_size 分块 (5546/200 = 28 块) 一次打出。
  任何 60s 滑动窗口至多一个脉冲 (28 < 48 安全 rpm); 单块失败不拖垮整轮,
  失败块单独重试一次 (见 fetch_intraday_full_market_burst)。
- 稳态轮单请求: 无脉冲并发, 间隔可低至 3s; 实际节奏 = max(间隔, 单轮完成),
  服务端响应 ~5s 时自动退化为响应节奏, 不会重叠请求。
- 固定节奏: 默认 6s 一轮 (clamp [3, 300]), 不补跑 (missed 轮次直接跳过)。
- 仅连续竞价时段运行 (9:30-11:30 / 13:00-15:00), 午休/收盘自动暂停与恢复;
  午休后恢复因覆盖滞后会多跑一次修复轮, 幂等无害。
- 不与其他分钟能力冲突: 与 盘后分钟同步 (kline.minute.batch) / 分时监控路径
  分属不同限流池; 落盘走 _write_minute_partition 的 unique(symbol,datetime)
  合并, 与盘后同步写同一分区安全幂等。
- 数据源插件化让位: 配置了自定义分钟源 (minute_data_provider != tickflow) 时
  服务不启动 — 盘中增量交由插件自管, 本服务不抢占。

分层: 本模块只做调度/落盘/状态; TickFlow SDK 调用全部在 kline_sync 边界层
(fetch_intraday_full_market_burst / fetch_intraday_universe_increment),
保持插件化边界不泄漏。
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from app.market_time import cn_now, cn_today, in_continuous_session
from app.services import preferences

# 轮询间隔允许范围 (秒): 稳态轮单请求无并发脉冲, 下限 3s;
# 上限 120s — universe 端点每标的只回最新 3 根, 间隔超过 3 分钟必留缺口,
# 每轮都会触发修复轮, 稳态设计失效, 故不允许配到 120s 以上。
REFRESH_INTERVAL_MIN = 3
REFRESH_INTERVAL_MAX = 120
# 等待步长 (秒): 循环小步睡眠, 便于快速停止与偏好热生效。
_LOOP_STEP_S = 2.0
# 当日覆盖滞后超过该分钟数 (≈ universe 单请求 3 根余量) → 触发全天修复轮。
_REPAIR_LAG_MINUTES = 3.0
# 连续空轮达到该次数 → 强制全天修复轮 (自愈 universe 端点持续异常)。
_EMPTY_ROUNDS_TO_REPAIR = 2


def _in_continuous_session(now=None) -> bool:
    """A股连续竞价时段 (北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。"""
    return in_continuous_session(now)


@dataclass
class _RefreshState:
    """服务运行状态 (status() 的内存镜像, 循环线程内更新)。"""

    rounds: int = 0
    last_round_at: float | None = None      # epoch 秒
    last_round_ms: float | None = None      # 单轮耗时
    last_rows: int = 0                      # 上轮写入行数 (合并后)
    last_symbols: int = 0                   # 上轮覆盖标的数
    last_requests: int = 0                  # 上轮请求数 (增量恒 1, 修复=分块数+重试)
    last_mode: str | None = None            # 上轮模式: "increment" / "full"
    last_error: str | None = None
    next_round_at: float | None = None      # epoch 秒
    extra: dict[str, Any] = field(default_factory=dict)


class MinuteRefreshService:
    """盘中分钟增量刷新: 单实例挂 app.state.minute_refresh, 后台守护线程。"""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._app_state: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = _RefreshState()
        self._round_lock = threading.Lock()  # 同时只允许一轮 (手动触发与定时轮互斥)
        self._empty_rounds = 0               # 连续空轮计数 (escalate 到全天修复)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def set_repo(self, repo) -> None:
        self._repo = repo

    def set_app_state(self, app_state: Any) -> None:
        self._app_state = app_state

    def start(self) -> bool:
        """启动后台线程 (幂等)。开关/时段/能力判断都在循环内每轮做, 热生效。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="minute-refresh", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 门控
    # ------------------------------------------------------------------

    def capability_ok(self) -> bool:
        """Cap.INTRADAY_UNIVERSE (全量分钟) 存在。能力探测结果缓存在 app.state。

        门控挂在稳态增量的主能力上; 全天修复轮用的 intraday.batch 与其
        同属 Expert 档 (tiers.yaml), 目前两者必然同时持有。
        """
        capset = getattr(self._app_state, "capabilities", None) if self._app_state else None
        if capset is None:
            return False
        try:
            from app.tickflow.capabilities import Cap

            return capset.has(Cap.INTRADAY_UNIVERSE)
        except Exception:
            return False

    def custom_provider_active(self) -> bool:
        """配置了自定义分钟源 → 让位插件, 本服务不启动。"""
        try:
            return preferences.get_minute_data_provider() != "tickflow"
        except Exception:
            return False

    def _gate_reason(self) -> str | None:
        """返回本轮不执行的原因 (None = 放行)。"""
        if not preferences.get_minute_refresh_enabled():
            return "disabled"
        if self.custom_provider_active():
            return "custom_minute_provider"
        if not self.capability_ok():
            return "capability"
        if not _in_continuous_session():
            return "outside_trading_hours"
        # 节假日 (工作日但休市): 周几门控覆盖不到, 由交易日探针剔除。
        # 未知 (None) 维持现状 — 空轮升级机制兜底 (探针失灵时的第二道防线)。
        from app.services import trading_day

        if trading_day.is_trading_day() is False:
            return "holiday"
        return None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._gate_reason()
                if reason is None:
                    interval = preferences.get_minute_refresh_interval()
                    started = time.time()
                    self._run_round()
                    # 固定节奏: 下一轮 = max(本轮起点+间隔, 本轮完成), 不补跑
                    finish = time.time()
                    self._state.next_round_at = max(started + interval, finish)
                    # 等到下一轮 (小步睡眠保持可停/偏好热切换)
                    while not self._stop.is_set():
                        now = time.time()
                        gate = self._gate_reason()
                        if gate is not None:
                            self._state.next_round_at = None
                            break  # 门控关闭 → 回外层等待重评估
                        if now >= self._state.next_round_at:
                            break
                        self._stop.wait(min(_LOOP_STEP_S, max(0.0, self._state.next_round_at - now)))
                    continue
            except Exception as e:
                self._state.last_error = f"round failed: {e}"
            self._stop.wait(_LOOP_STEP_S)

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------

    def _today_coverage_lag_minutes(self) -> float | None:
        """当日分区最新K距现在的分钟数; None = 当日无数据。

        覆盖度探测只读当日分区文件 (毫秒级); 任何异常按无数据处理 →
        本轮走全天修复, 不会因探测失败而丢增量。
        """
        with contextlib.suppress(Exception):
            part = (
                self._repo.store.data_dir / "kline_minute"
                / f"date={cn_today().isoformat()}" / "part.parquet"
            )
            if not part.exists():
                return None
            mx = pl.read_parquet(part, columns=["datetime"])["datetime"].max()
            if mx is None:
                return None
            # 分区 datetime 为北京墙钟 naive, cn_now 带时区 → 剥齐再比
            return (cn_now().replace(tzinfo=None) - mx).total_seconds() / 60
        return None

    def _select_mode(self) -> str:
        """选轮次模式: 稳态增量 (universe 单请求) vs 全天修复 (burst 脉冲)。

        当日已有数据且覆盖滞后 ≤ _REPAIR_LAG_MINUTES (≈ 3 根余量) → 增量;
        冷启动 / 断档超阈值 / 连续空轮 → 全天修复。
        """
        if self._empty_rounds >= _EMPTY_ROUNDS_TO_REPAIR:
            return "full"
        lag = self._today_coverage_lag_minutes()
        if lag is None or lag > _REPAIR_LAG_MINUTES:
            return "full"
        return "increment"

    def _run_round(self) -> None:
        from app.services import kline_sync

        t0 = time.perf_counter()
        mode = self._select_mode()
        with self._round_lock:
            if mode == "increment":
                df, requests = kline_sync.fetch_intraday_universe_increment()
                self._state.last_symbols = (
                    df["symbol"].n_unique() if not df.is_empty() else 0
                )
            else:
                symbols = self._universe()
                self._state.last_symbols = len(symbols)
                if not symbols:
                    self._state.last_error = "empty universe (instruments 未加载)"
                    return
                capset = getattr(self._app_state, "capabilities", None) if self._app_state else None
                df, requests = kline_sync.fetch_intraday_full_market_burst(symbols, capset)
            self._state.last_requests = requests
            if df.is_empty():
                self._empty_rounds += 1
                self._state.last_error = f"intraday {mode} returned no data"
                return
            self._empty_rounds = 0
            written = kline_sync._write_minute_partition(
                df, self._repo.store.data_dir / "kline_minute",
            )

        self._state.rounds += 1
        self._state.last_round_at = time.time()
        self._state.last_round_ms = (time.perf_counter() - t0) * 1000
        self._state.last_rows = written
        self._state.last_mode = mode
        self._state.last_error = None

    def _universe(self) -> list[str]:
        """全市场 A 股标的 (instruments 维表, 与盘后分钟同步同一来源)。"""
        inst = self._repo.get_instruments()
        if inst.is_empty() or "symbol" not in inst.columns:
            return []
        return inst["symbol"].cast(pl.Utf8).unique().sort().to_list()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        import contextlib

        with contextlib.suppress(Exception):
            enabled = preferences.get_minute_refresh_enabled()
        running = self._thread is not None and self._thread.is_alive()
        gate = self._gate_reason()
        return {
            "enabled": enabled,
            "running": running,
            "interval_seconds": preferences.get_minute_refresh_interval(),
            "capability_ok": self.capability_ok(),
            "custom_provider_active": self.custom_provider_active(),
            "in_trading_hours": _in_continuous_session(),
            "gate_reason": gate if (enabled and running) else (gate or "disabled"),
            "rounds": self._state.rounds,
            "last_round_at": self._state.last_round_at,
            "last_round_ms": self._state.last_round_ms,
            "last_rows": self._state.last_rows,
            "last_symbols": self._state.last_symbols,
            "last_requests": self._state.last_requests,
            "last_mode": self._state.last_mode,
            "next_round_at": self._state.next_round_at,
            "last_error": self._state.last_error,
        }

    def trigger_manual_round(self) -> dict[str, Any]:
        """手动触发一轮 (无视时段门控, 但仍受能力/插件门控); 供状态页「立即刷新」。"""
        if self.custom_provider_active() or not self.capability_ok():
            return {"ok": False, "reason": self._gate_reason() or "capability"}
        threading.Thread(target=self._run_round, daemon=True, name="minute-refresh-manual").start()
        return {"ok": True}
