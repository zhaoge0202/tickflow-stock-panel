"""候选池高频盯盘（A 方案：全市场低频选池 + 候选池高频盯盘）。

为什么需要它
------------
全市场 5579 只在代理链路下单轮取数 12~24s，做不到 3~6 秒一轮；而"拉直线"这类
信号只在几十秒内有效（样本：2026-11-04 桂林三金 09:41 推送 → 09:42:23 封板，
窗口 82 秒）。把高频率只用在候选池（≤80 只）上：一次 batch-quote 约 0.6s，
3~6 秒一轮完全可行。

两层结构
--------
低频（默认 60s）：全市场按"定义域"收敛候选池
    沪深主板 / 价格≥2 / 历史≥100 根 / 近 20 日无涨停 / 20 日均量÷60 日均量≤1.0
高频（默认 5s）：候选池逐轮拉行情，检测 N 秒涨速 → 触发即推送

与策略监控的区别
----------------
这是**盘口异动预警**，不是策略信号：不做策略回测口径的信号计算，只回答
"这只候选票正在被快速拉升"。策略信号仍由监控中心 MonitorRuleEngine 产出。

配置（环境变量，全部可回退）
--------------------------
CANDIDATE_WATCH_ENABLED=1        总开关
CANDIDATE_WATCH_INTERVAL_S=5     盯盘周期
CANDIDATE_WATCH_WINDOW_S=6       涨速统计窗口
CANDIDATE_WATCH_BURST_PCT=1.2    窗口内涨幅阈值(%)
CANDIDATE_WATCH_NEAR_LIMIT_PCT=3 距涨停百分内才提醒(%)
CANDIDATE_WATCH_COOLDOWN_S=120   同票重复提醒冷却
CANDIDATE_WATCH_POOL_MAX=80      候选池上限（=一次 batch-quote 的量）
CANDIDATE_WATCH_POOL_REFRESH_S=60 候选池刷新周期
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from datetime import date

import polars as pl

logger = logging.getLogger(__name__)

# 定义域参数（与「拉直线追强」策略一致，改策略参数时这里要同步）
_DRY_SHRINK_MAX = 1.0        # 20 日均量 / 60 日均量 上限（地量横盘）
_NO_LIMIT_DAYS = 20          # 近 N 日无涨停
_MAX_CHG20 = 0.095           # 近 20 日最大单日涨幅上限（涨停判定）
_MIN_BARS = 100              # 历史最少 bar 数（排除次新）
_MIN_PRICE = 2.0
_MAIN_BOARD_PREFIX = ("60", "00")  # 沪主板/深主板


def _env_float(key: str, default: float) -> float:
    raw = (os.getenv(key) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = (os.getenv(key) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


class CandidateWatchService:
    """候选池高频盯盘。线程模型：池维护线程 + 盯盘线程，均由 start()/stop() 管理。"""

    DOMAIN_LOOKBACK_DAYS = 150

    def __init__(self) -> None:
        self._repo = None
        self._app_state = None
        self._running = False
        self._lock = threading.Lock()

        self._enabled = (os.getenv("CANDIDATE_WATCH_ENABLED", "1") or "1") != "0"
        self._interval = max(1.0, _env_float("CANDIDATE_WATCH_INTERVAL_S", 5.0))
        self._window = max(2.0, _env_float("CANDIDATE_WATCH_WINDOW_S", 6.0))
        self._burst_pct = _env_float("CANDIDATE_WATCH_BURST_PCT", 1.2)
        self._near_limit_pct = _env_float("CANDIDATE_WATCH_NEAR_LIMIT_PCT", 3.0)
        self._cooldown = max(10.0, _env_float("CANDIDATE_WATCH_COOLDOWN_S", 120.0))
        self._pool_max = max(1, _env_int("CANDIDATE_WATCH_POOL_MAX", 80))
        self._pool_refresh_s = max(10.0, _env_float("CANDIDATE_WATCH_POOL_REFRESH_S", 60.0))

        # 定义域（按日缓存，只读收盘日数据）
        self._domain_date: date | None = None
        self._domain: dict[str, float] = {}       # symbol → 昨日收盘（用于涨停价）
        # 候选池
        self._pool: list[str] = []
        self._pool_built_at = 0.0
        # 盯盘状态
        self._hist: dict[str, collections.deque] = {}
        self._last_fire: dict[str, float] = {}
        self._last_cycle_ms = 0.0
        self._last_trigger: dict | None = None
        self._triggers_today = 0
        self._trigger_day: date | None = None
        self._threads: list[threading.Thread] = []
        self._stop_evt = threading.Event()

    # ── 装配 ────────────────────────────────────────────────
    def set_repo(self, repo) -> None:
        self._repo = repo

    def set_app_state(self, app_state) -> None:
        self._app_state = app_state

    # ── 生命周期 ────────────────────────────────────────────
    def start(self) -> bool:
        if self._running or not self._enabled:
            return False
        self._stop_evt.clear()
        self._running = True
        for target, name in ((self._pool_loop, "cand-pool"), (self._watch_loop, "cand-watch")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        logger.info(
            "候选池盯盘已启动: 周期 %.0fs / 窗口 %.0fs / 涨速阈值 %.1f%% / 距涨停 %.1f%% 内 / 池上限 %d",
            self._interval, self._window, self._burst_pct, self._near_limit_pct, self._pool_max,
        )
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_evt.set()
        for th in self._threads:
            th.join(timeout=5)
        self._threads.clear()
        logger.info("候选池盯盘已停止")

    # ── 循环 ────────────────────────────────────────────────
    def _pool_loop(self) -> None:
        while self._running and not self._stop_evt.is_set():
            try:
                if self._is_watch_window():
                    self._refresh_pool()
            except Exception:  # noqa: BLE001
                logger.exception("候选池刷新异常")
            self._stop_evt.wait(self._pool_refresh_s)

    def _watch_loop(self) -> None:
        while self._running and not self._stop_evt.is_set():
            started = time.perf_counter()
            try:
                if self._is_watch_window() and self._pool:
                    self._watch_once()
            except Exception:  # noqa: BLE001
                logger.exception("候选池盯盘异常")
            self._last_cycle_ms = (time.perf_counter() - started) * 1000
            elapsed = time.perf_counter() - started
            self._stop_evt.wait(max(0.0, self._interval - elapsed))

    # ── 候选池 ──────────────────────────────────────────────
    def _refresh_pool(self) -> None:
        repo = self._repo
        if repo is None:
            return
        today = self._cn_today()
        if self._domain_date != today:
            self._domain = self._build_domain(today)
            self._domain_date = today
        if not self._domain:
            self._pool = []
            return
        enriched, edate = repo.get_enriched_latest()
        if enriched is None or enriched.is_empty() or edate != today:
            return
        cols = [c for c in ("symbol", "close") if c in enriched.columns]
        if len(cols) < 2:
            return
        cur = dict(zip(enriched["symbol"].to_list(), enriched["close"].to_list(), strict=False))
        pool: list[str] = []
        for sym, prev_close in self._domain.items():
            price = cur.get(sym)
            if price is None or price <= 0 or prev_close <= 0:
                continue
            limit_up = prev_close * 1.10
            if price >= limit_up * 0.999:      # 已封板不再盯
                continue
            pool.append(sym)
        # 距涨停越近越优先（只保留上限内的）
        pool.sort(key=lambda s: (cur.get(s, 0) / (self._domain[s] * 1.10)), reverse=True)
        with self._lock:
            self._pool = pool[: self._pool_max]
            self._pool_built_at = time.time()
        logger.info(
            "候选池刷新: 定义域 %d 只 → 未封板 %d 只 → 盯盘 %d 只",
            len(self._domain), len(pool), len(self._pool),
        )

    def _build_domain(self, today: date) -> dict[str, float]:
        """按收盘日数据算定义域：地量横盘 + 近20日无涨停 + 主板 + 非次新。"""
        repo = self._repo
        store = getattr(repo, "store", None)
        if store is None:
            return {}
        base = store.data_dir / "kline_daily_enriched"
        if not base.exists():
            return {}
        parts = sorted(base.glob("date=*/part.parquet"))
        parts = [p for p in parts if p.parent.name[5:] < today.isoformat()][-self.DOMAIN_LOOKBACK_DAYS:]
        if not parts:
            return {}
        try:
            from app.parquet import scan_enriched_parquet
            lf = scan_enriched_parquet([str(p) for p in parts]).select(["symbol", "date", "close", "volume"])
            df = lf.collect(engine="streaming").sort(["symbol", "date"])
        except Exception:  # noqa: BLE001
            logger.exception("候选池定义域读取失败")
            return {}
        if df.is_empty():
            return {}
        df = df.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("chg"))
        agg = df.group_by("symbol").agg([
            pl.col("volume").tail(20).mean().alias("v20"),
            pl.col("volume").tail(60).mean().alias("v60"),
            pl.col("chg").tail(_NO_LIMIT_DAYS).max().alias("max_chg"),
            pl.col("close").last().alias("prev_close"),
            pl.len().alias("bars"),
        ])
        agg = agg.filter(
            (pl.col("bars") >= _MIN_BARS)
            & (pl.col("prev_close") >= _MIN_PRICE)
            & (pl.col("v60") > 0)
            & ((pl.col("v20") / pl.col("v60")) <= _DRY_SHRINK_MAX)
            & (pl.col("max_chg").fill_null(0.0) <= _MAX_CHG20)
        )
        out: dict[str, float] = {}
        for sym, pc in zip(agg["symbol"].to_list(), agg["prev_close"].to_list(), strict=False):
            s = str(sym)
            code = s.split(".")[0]
            if not code.startswith(_MAIN_BOARD_PREFIX):
                continue
            try:
                out[s] = float(pc)
            except (TypeError, ValueError):
                continue
        logger.info("候选池定义域计算完成: %d 只 (基于 %d 个交易日分区)", len(out), len(parts))
        return out

    # ── 盯盘 ────────────────────────────────────────────────
    def _watch_once(self) -> None:
        with self._lock:
            pool = list(self._pool)
        if not pool:
            return
        rows = self._fetch_pool_quotes(pool)
        if not rows:
            return
        now = time.time()
        for row in rows:
            sym = str(row.get("symbol") or "").strip()
            price = row.get("last_price")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            if not sym or not price or price <= 0:
                continue
            prev_close = self._domain.get(sym)
            if not prev_close:
                continue
            hist = self._hist.setdefault(sym, collections.deque(maxlen=64))
            hist.append((now, price))
            # 取窗口起点：最接近 now - window 的样本
            base_price = None
            for ts, px in hist:
                if now - ts <= self._window + 0.5:
                    base_price = px
                    break
            if base_price is None or len(hist) < 2:
                continue
            burst = (price / base_price - 1) * 100
            limit_up = prev_close * 1.10
            to_limit = (limit_up / price - 1) * 100
            if burst < self._burst_pct:
                continue
            if to_limit < 0 or to_limit > self._near_limit_pct:
                continue
            last = self._last_fire.get(sym, 0.0)
            if now - last < self._cooldown:
                continue
            self._last_fire[sym] = now
            self._emit(sym, price, prev_close, burst, to_limit, row)

    def _fetch_pool_quotes(self, pool: list[str]) -> list[dict]:
        try:
            from app.data_providers import custom as custom_sources
            from app.services import preferences

            name = preferences.get_realtime_data_provider()
            if name == "tickflow" or not custom_sources.provider_has_dataset(name, "realtime"):
                return []
            provider = custom_sources.get_provider(name)
            return list(provider.get_realtime(symbols=pool) or [])
        except Exception:  # noqa: BLE001
            logger.warning("候选池取数失败", exc_info=True)
            return []

    def _emit(self, sym: str, price: float, prev_close: float,
              burst: float, to_limit: float, row: dict) -> None:
        change_pct = price / prev_close - 1 if prev_close else 0.0
        name = str(row.get("name") or sym)
        msg = (
            f"候选池异动 {name} {burst:+.1f}%/{self._window:.0f}s "
            f"现价 {price:.2f} 距涨停 {to_limit:.1f}%"
        )
        alert = {
            "source": "candidate_watch",
            "type": "burst",
            "rule_id": "candidate_watch",
            "strategy_id": None,
            "symbol": sym,
            "name": name,
            "message": msg,
            "price": price,
            "change_pct": change_pct,
            "signals": ["burst_speed"],
            "severity": "info",
            "conditions": [],
            "logic": "and",
            "burst_pct": round(burst, 2),
            "to_limit_pct": round(to_limit, 2),
        }
        today = self._cn_today()
        if self._trigger_day != today:
            self._trigger_day = today
            self._triggers_today = 0
        self._triggers_today += 1
        self._last_trigger = {"symbol": sym, "name": name, "ts": time.time(),
                             "burst_pct": burst, "to_limit_pct": to_limit}
        logger.info("候选池触发: %s", msg)
        qs = getattr(self._app_state, "quote_service", None) if self._app_state else None
        store = getattr(getattr(self._repo, "store", None), "data_dir", None)
        try:
            from app.services import alert_store
            if store is not None:
                alert_store.append_many(store, [alert])
        except Exception:  # noqa: BLE001
            logger.warning("候选池告警落盘失败", exc_info=True)
        if qs is not None:
            try:
                qs._broadcast_alerts([alert])
                qs._maybe_send_system_notifications([alert])
            except Exception:  # noqa: BLE001
                logger.warning("候选池告警推送失败", exc_info=True)

    # ── 工具 ────────────────────────────────────────────────
    @staticmethod
    def _cn_today() -> date:
        from app.market_time import cn_today
        return cn_today()

    @staticmethod
    def _is_watch_window() -> bool:
        """只在连续竞价时段盯盘（与监控中心同口径）。"""
        from app.services.quote_service import QuoteService
        return QuoteService._is_continuous_trading()

    def snapshot(self) -> dict:
        with self._lock:
            pool = list(self._pool)
        return {
            "enabled": self._enabled,
            "running": self._running,
            "interval_s": self._interval,
            "window_s": self._window,
            "burst_pct": self._burst_pct,
            "near_limit_pct": self._near_limit_pct,
            "cooldown_s": self._cooldown,
            "pool_size": len(pool),
            "pool_max": self._pool_max,
            "pool_built_ago_s": round(time.time() - self._pool_built_at, 1) if self._pool_built_at else None,
            "domain_size": len(self._domain),
            "last_cycle_ms": round(self._last_cycle_ms, 1),
            "triggers_today": self._triggers_today,
            "last_trigger": self._last_trigger,
            "pool_preview": pool[:10],
        }


_service: CandidateWatchService | None = None


def get_candidate_watch() -> CandidateWatchService:
    global _service
    if _service is None:
        _service = CandidateWatchService()
    return _service
