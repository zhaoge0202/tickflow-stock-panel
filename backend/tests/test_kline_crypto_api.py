"""crypto 资产类别的 K 线 API 分支 (P2-A)。

口径 (app/market_rules + CONTRIBUTING §3):
  - 「今天」是合约日 = 北京自然日 (OKX 官方日线以 UTC+8 零点切日, 见 market_rules
    选源约束), 不是服务器本地日期;
  - 合约日 D = 北京墙钟 [D 00:00, D+1 00:00), 分钟 datetime 存北京墙钟 naive;
  - 无涨跌停/复权/股本 → price_limit 恒 None; 完整性阈值 1440 根/合约日;
  - crypto 实时链路 P4 才落地 → 本层不得发起任何上游请求 (fetch_minute_single /
    sync_daily_batch / 实时蜡烛注入都不能被调用)。

假 request / app.state 的构造方式沿用 tests/test_daily_batch_asset.py。
"""
from __future__ import annotations

import datetime as dt
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.api import kline
from app.market_rules import CRYPTO
from app.market_time import CN_TZ

CRYPTO_SYMBOL = "SOXLUSDT"
STOCK_SYMBOL = "600519.SH"

# get_minute 入口会读 repo.store.data_dir 判定分钟口径 (raw_basis, 新主干行为)。
# 桩给一个不存在的目录 = 未迁移口径, 与存量 A 股库现状一致。
_STUB_DATA_DIR = Path("/nonexistent/tickflow-crypto-api-stub-data")


class _FrozenDate(dt.date):
    """服务器本地日期桩 (与合约日/北京日期都不同, 用于证明默认窗口不取本地日期)。"""

    @classmethod
    def today(cls) -> dt.date:
        return cls(2026, 10, 20)


class _Repo:
    """K 线 API crypto/A 股分支读到的仓储桩, 记录调用参数供断言。"""

    def __init__(
        self,
        *,
        asset_type: str = CRYPTO,
        minute_by_day: dict[dt.date, pl.DataFrame] | None = None,
        minute_batch_frame: pl.DataFrame | None = None,
        minute_range_frame: pl.DataFrame | None = None,
        daily: pl.DataFrame | None = None,
        instruments: pl.DataFrame | None = None,
    ) -> None:
        self.asset_type = asset_type
        self._minute_by_day = minute_by_day or {}
        self._minute_batch_frame = minute_batch_frame
        self._minute_range_frame = minute_range_frame
        self._daily = daily
        self._instruments = instruments if instruments is not None else pl.DataFrame()
        self.store = SimpleNamespace(data_dir=_STUB_DATA_DIR)
        self.minute_calls: list[tuple[str, dt.date, str]] = []
        self.minute_batch_calls: list[tuple[list[str], dt.date, str]] = []
        self.minute_range_calls: list[tuple[list[str], dt.date, dt.date, str]] = []
        self.crypto_daily_calls: list[tuple[str, dt.date, dt.date]] = []
        self.daily_asset_calls: list[tuple[str, str, dt.date, dt.date]] = []

    def resolve_asset_type(self, symbol: str) -> str:
        if symbol == CRYPTO_SYMBOL:
            return CRYPTO
        if symbol == STOCK_SYMBOL:
            return "stock"
        return self.asset_type

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        return self._instruments if asset_type == self.asset_type else pl.DataFrame()

    def get_instruments(self) -> pl.DataFrame:
        return self._instruments if self.asset_type == "stock" else pl.DataFrame()

    def get_etf_symbol_set(self) -> set[str]:
        return set()

    def get_minute(self, symbol: str, trade_date: dt.date, asset_type: str = "stock") -> pl.DataFrame:
        self.minute_calls.append((symbol, trade_date, asset_type))
        return self._minute_by_day.get(trade_date, pl.DataFrame())

    def get_minute_batch(self, symbols: list[str], trade_date: dt.date, asset_type: str = "stock") -> pl.DataFrame:
        self.minute_batch_calls.append((list(symbols), trade_date, asset_type))
        return self._minute_batch_frame if self._minute_batch_frame is not None else pl.DataFrame()

    def get_minute_range(self, symbols: list[str], start: dt.date, end: dt.date, asset_type: str = "stock") -> pl.DataFrame:
        self.minute_range_calls.append((list(symbols), start, end, asset_type))
        return self._minute_range_frame if self._minute_range_frame is not None else pl.DataFrame()

    def _daily_window(self, start: dt.date, end: dt.date) -> pl.DataFrame:
        if self._daily is None or self._daily.is_empty():
            return pl.DataFrame()
        return self._daily.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    def get_daily_asset(self, asset_type: str, symbol: str, start: dt.date, end: dt.date, columns=None) -> pl.DataFrame:
        self.daily_asset_calls.append((asset_type, symbol, start, end))
        return self._daily_window(start, end)

    def get_crypto_daily(self, symbol: str, start: dt.date, end: dt.date, columns=None) -> pl.DataFrame:
        self.crypto_daily_calls.append((symbol, start, end))
        return self._daily_window(start, end)


def _request(repo: _Repo, *, capabilities=None):
    # 真实 app.state 恒有 capabilities (get_minute 入口读它); 默认给空集, 需要断言
    # 具体能力时由用例显式传入。
    state = SimpleNamespace(
        repo=repo,
        capabilities=capabilities if capabilities is not None else SimpleNamespace(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _forbidden(message: str) -> MagicMock:
    return MagicMock(side_effect=AssertionError(message))


def _minute_rows(start: datetime, count: int, symbol: str = CRYPTO_SYMBOL) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol] * count,
        "datetime": [start + timedelta(minutes=i) for i in range(count)],
        "open": [10.0] * count,
        "high": [10.5] * count,
        "low": [9.5] * count,
        "close": [10.0] * count,
        "volume": [1.0] * count,
        "amount": [10.0] * count,
    })


def _daily_frame(rows: list[tuple[dt.date, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "date": [day for day, _ in rows],
        "close": [close for _, close in rows],
    })


def _capset() -> SimpleNamespace:
    return SimpleNamespace(has=lambda cap: True, limits=lambda cap: None)


@pytest.fixture(autouse=True)
def _plain_transport(monkeypatch: pytest.MonkeyPatch):
    """传输压缩不参与本文件断言: 请求桩无 headers, 与生产行为一致返回未压缩 dict。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: False)
    monkeypatch.setattr(preferences, "get_daily_batch_compress", lambda: False)


# ---------------- /minute: 合约日归属 / 不补拉 / 无涨跌停 ----------------


def test_minute_default_date_is_contract_day_without_session_probe(monkeypatch):
    """未传 date → 当前 UTC 合约日; 24x7 不做周末/休市探针回退, 也不实时补拉。"""
    bars = _minute_rows(datetime(2026, 10, 13, 0, 0), 3)
    repo = _Repo(minute_by_day={dt.date(2026, 10, 13): bars})
    # 北京 10-13 06:00 → 合约日 10-13 (服务器本地日期桩是 10-20, 证明不取本地日期)
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 10, 13, 6, 0, tzinfo=CN_TZ))
    monkeypatch.setattr(kline, "cn_today", lambda: dt.date(2026, 10, 13))
    monkeypatch.setattr(kline.trading_day, "is_trading_day", _forbidden("crypto 不应做交易日探针"))
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", _forbidden("crypto 不应实时补拉"))

    resp = kline.get_minute(_request(repo), CRYPTO_SYMBOL, None, live=False)

    assert resp["date"] == "2026-10-13"
    assert resp["asset_type"] == "crypto"
    assert resp["source"] == "crypto_local"
    assert [row["datetime"] for row in resp["rows"]] == [
        datetime(2026, 10, 13, 0, 0) + timedelta(minutes=i) for i in range(3)
    ]
    # 日归属与仓储过滤同口径 (北京日) → 单次读取即可覆盖整日
    assert repo.minute_calls == [(CRYPTO_SYMBOL, dt.date(2026, 10, 13), CRYPTO)]


def test_minute_slices_rows_to_contract_day_window_and_reads_prev_close(monkeypatch):
    """只回合约日窗口内的行: 跨日混入的行被裁掉 (防御 provider 错日), 本日行保留。"""
    stray = _minute_rows(datetime(2026, 10, 11, 23, 59), 1)   # 上一日 (错日混入)
    own_day = _minute_rows(datetime(2026, 10, 12, 0, 0), 1)   # 合约日 10-12 起点
    noon = _minute_rows(datetime(2026, 10, 12, 12, 0), 1)     # 合约日 10-12
    repo = _Repo(
        minute_by_day={
            dt.date(2026, 10, 12): pl.concat([stray, own_day, noon]),
        },
        daily=_daily_frame([
            (dt.date(2026, 10, 8), 1.0),   # 窗口外 (D-5)
            (dt.date(2026, 10, 10), 2.0),
            (dt.date(2026, 10, 11), 3.0),  # 上一合约日 → prev_close
            (dt.date(2026, 10, 12), 4.0),  # 当日, 不算 prev_close
        ]),
    )

    resp = kline.get_minute(_request(repo), CRYPTO_SYMBOL, dt.date(2026, 10, 12), live=False)

    assert [row["datetime"] for row in resp["rows"]] == [
        datetime(2026, 10, 12, 0, 0), datetime(2026, 10, 12, 12, 0),
    ]
    assert resp["prev_close"] == 3.0
    assert repo.crypto_daily_calls == [(CRYPTO_SYMBOL, dt.date(2026, 10, 9), dt.date(2026, 10, 11))]


def test_minute_prev_close_is_none_without_previous_contract_day(monkeypatch):
    repo = _Repo(minute_by_day={dt.date(2026, 10, 12): _minute_rows(datetime(2026, 10, 12, 8, 0), 1)})

    resp = kline.get_minute(_request(repo), CRYPTO_SYMBOL, dt.date(2026, 10, 12), live=False)

    assert resp["prev_close"] is None


def test_minute_price_limit_is_none_and_a_share_rules_not_consulted(monkeypatch):
    """无涨跌停概念: price_limit 显式 None; 不调 A 股的涨跌停/前收链路, live 参数被忽略。"""
    repo = _Repo(
        minute_by_day={dt.date(2026, 10, 12): _minute_rows(datetime(2026, 10, 12, 8, 0), 2)},
        daily=_daily_frame([(dt.date(2026, 10, 11), 7.5)]),
    )
    monkeypatch.setattr(kline, "_get_price_limit_info", _forbidden("crypto 无涨跌停概念"))
    monkeypatch.setattr(kline, "_get_previous_closes", _forbidden("crypto 上一合约日收盘走 get_crypto_daily"))
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", _forbidden("live 对 crypto 无效"))

    resp = kline.get_minute(_request(repo), CRYPTO_SYMBOL, dt.date(2026, 10, 12), live=True)

    assert resp["price_limit"] is None
    assert resp["prev_close"] == 7.5
    assert resp["source"] == "crypto_local"


def test_minute_expected_bars_use_1440_per_contract_day(monkeypatch):
    """完整性阈值 1440 根/合约日 (不是 A 股 240); 当前合约日按已过分钟折算。"""
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 10, 13, 6, 0, tzinfo=CN_TZ))
    history = dt.date(2026, 10, 10)

    partial = _Repo(minute_by_day={history: _minute_rows(datetime(2026, 10, 10, 0, 0), 100)})
    partial_resp = kline.get_minute(_request(partial), CRYPTO_SYMBOL, history, live=False)
    assert partial_resp["expected_bars"] == 1440
    assert partial_resp["is_complete"] is False

    full = _Repo(minute_by_day={history: _minute_rows(datetime(2026, 10, 10, 0, 0), 1440)})
    full_resp = kline.get_minute(_request(full), CRYPTO_SYMBOL, history, live=False)
    assert full_resp["expected_bars"] == 1440
    assert full_resp["is_complete"] is True

    # 当前合约日 (北京 10-13 06:00 → 合约日 10-13): 从北京 00:00 起已过 6h = 360 分钟
    current = _Repo(minute_by_day={dt.date(2026, 10, 13): _minute_rows(datetime(2026, 10, 13, 0, 0), 10)})
    current_resp = kline.get_minute(_request(current), CRYPTO_SYMBOL, None, live=False)
    assert current_resp["date"] == "2026-10-13"
    assert current_resp["expected_bars"] == 360
    assert current_resp["is_complete"] is False


def test_minute_without_local_data_reports_none_source(monkeypatch):
    repo = _Repo()

    resp = kline.get_minute(_request(repo), CRYPTO_SYMBOL, dt.date(2026, 10, 12), live=False)

    assert resp["rows"] == []
    assert resp["source"] == "none"
    assert resp["price_limit"] is None


# ---------------- /daily: 只回本地合约日K ----------------


def test_daily_crypto_uses_contract_day_default_and_skips_live_path(monkeypatch):
    local = pl.DataFrame({
        "symbol": [CRYPTO_SYMBOL],
        "date": [dt.date(2026, 10, 14)],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [1.0], "amount": [10.5],
    })
    repo = _Repo(daily=local)
    monkeypatch.setattr(kline, "date", _FrozenDate)  # 服务器本地日期 10-20
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 10, 15, 4, 0, tzinfo=CN_TZ))  # 合约日 10-15
    monkeypatch.setattr(kline, "_compute_live_daily_rows", _forbidden("crypto 不应实时补拉日K"))
    monkeypatch.setattr(kline, "_maybe_inject_live_candle", _forbidden("crypto 不应注入实时蜡烛"))
    monkeypatch.setattr(kline.kline_sync, "sync_daily_batch", _forbidden("crypto 不应走 TickFlow 单票补拉"))

    resp = kline.get_daily(_request(repo), CRYPTO_SYMBOL, 120, None, None, None)

    assert resp["source"] == "crypto_local"
    assert resp["rows"] == local.to_dicts()
    # 默认窗口右端 = 合约日 10-15 (北京自然日), 不是服务器本地日期 10-20
    assert repo.daily_asset_calls == [
        (CRYPTO, CRYPTO_SYMBOL, dt.date(2026, 10, 15) - timedelta(days=120), dt.date(2026, 10, 15))
    ]


def test_daily_crypto_empty_local_returns_empty_rows_not_error(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(kline, "_compute_live_daily_rows", _forbidden("crypto 不应实时补拉日K"))

    resp = kline.get_daily(_request(repo), CRYPTO_SYMBOL, 120, "2026-09-01", "2026-10-14", None)

    assert resp["rows"] == []
    assert resp["source"] == "crypto_local"


# ---------------- /minute-range: 按合约日逐日 ----------------


def test_minute_range_groups_sessions_by_contract_day(monkeypatch):
    rows = pl.DataFrame({
        "symbol": [CRYPTO_SYMBOL] * 6,
        "datetime": [
            datetime(2026, 10, 10, 23, 0),   # 合约日 10-10 → 窗口外, 裁掉
            datetime(2026, 10, 12, 0, 0),    # 合约日 10-12 起点
            datetime(2026, 10, 12, 12, 0),   # 合约日 10-12
            datetime(2026, 10, 12, 23, 59),  # 合约日 10-12 终点
            datetime(2026, 10, 13, 0, 0),    # 合约日 10-13 起点
            datetime(2026, 10, 13, 2, 0),    # 合约日 10-13
        ],
        "open": [1.0] * 6,
        "high": [1.0] * 6,
        "low": [1.0] * 6,
        "close": [1.0] * 6,
        "volume": [1.0] * 6,
        "amount": [1.0] * 6,
    })
    repo = _Repo(
        minute_range_frame=rows,
        daily=_daily_frame([
            (dt.date(2026, 10, 11), 7.0),   # 合约日 10-12 的上一合约日
            (dt.date(2026, 10, 12), 8.0),   # 合约日 10-13 的上一合约日
        ]),
    )
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 10, 13, 3, 0, tzinfo=CN_TZ))  # 合约日 10-13

    resp = kline.get_minute_range(_request(repo), CRYPTO_SYMBOL, 2)

    assert resp["asset_type"] == "crypto"
    assert resp["source"] == "local"
    # 会话 = 合约日 = 北京自然日 (与仓储过滤同口径, 无跨日补偿)
    assert [session["date"] for session in resp["sessions"]] == ["2026-10-12", "2026-10-13"]
    assert [session["prev_close"] for session in resp["sessions"]] == [7.0, 8.0]
    assert [[row["datetime"] for row in session["rows"]] for session in resp["sessions"]] == [
        [datetime(2026, 10, 12, 0, 0), datetime(2026, 10, 12, 12, 0), datetime(2026, 10, 12, 23, 59)],
        [datetime(2026, 10, 13, 0, 0), datetime(2026, 10, 13, 2, 0)],
    ]
    # 读取窗口: 北京日期 [first, last] 即可覆盖 (同口径)
    assert repo.minute_range_calls == [
        ([CRYPTO_SYMBOL], dt.date(2026, 10, 12), dt.date(2026, 10, 13), CRYPTO)
    ]


# ---------------- /minute-batch: crypto fail-closed ----------------


def _batch_request(repo: _Repo, *, capabilities=None):
    repo.store = SimpleNamespace(data_dir=Path("data"))
    return _request(repo, capabilities=capabilities if capabilities is not None else _capset())


def test_minute_batch_excludes_crypto_without_breaking_a_share(monkeypatch):
    """crypto 标的不进 A 股口径路径: data 回空 rows + unsupported_symbols 显式列出。"""
    stock_bars = _minute_rows(datetime(2026, 10, 12, 9, 31), 240, symbol=STOCK_SYMBOL)
    repo = _Repo(asset_type="stock", minute_batch_frame=stock_bars)
    monkeypatch.setattr(kline.kline_sync, "sync_minute_batch", _forbidden("本地完整不应补拉"))

    resp = kline.get_minute_batch(
        _batch_request(repo),
        {"symbols": [CRYPTO_SYMBOL, STOCK_SYMBOL], "date": "2026-10-12"},
    )
    body = json.loads(resp.body)

    assert body["unsupported_symbols"] == [CRYPTO_SYMBOL]
    assert body["data"][CRYPTO_SYMBOL] == []
    assert len(body["data"][STOCK_SYMBOL]) == 240
    # crypto 被剔除后才读 A 股分钟K: 仓储只见 A 股标的
    assert repo.minute_batch_calls == [([STOCK_SYMBOL], dt.date(2026, 10, 12), "stock")]


def test_minute_batch_crypto_only_returns_marker_and_no_read(monkeypatch):
    repo = _Repo(asset_type="stock")
    monkeypatch.setattr(kline.kline_sync, "sync_minute_batch", _forbidden("crypto 不应补拉"))

    resp = kline.get_minute_batch(
        _batch_request(repo),
        {"symbols": [CRYPTO_SYMBOL], "date": "2026-10-12"},
    )

    assert resp["data"] == {CRYPTO_SYMBOL: []}
    assert resp["unsupported_symbols"] == [CRYPTO_SYMBOL]
    assert repo.minute_batch_calls == []


# ---------------- /instruments/search: crypto 可搜 ----------------


def test_instrument_search_returns_crypto_when_asset_type_requested():
    instruments = pl.DataFrame({
        "symbol": [CRYPTO_SYMBOL], "name": ["SOXL"], "code": [CRYPTO_SYMBOL],
    })
    repo = _Repo(asset_type=CRYPTO, instruments=instruments)

    hit = kline.search_instruments(_request(repo), "SOXL", 20, "crypto")

    assert hit["results"] == [
        {"symbol": CRYPTO_SYMBOL, "name": "SOXL", "code": CRYPTO_SYMBOL, "asset_type": "crypto"}
    ]
    # 默认仍是 stock: crypto 只在调用方显式传 asset_types 时出现
    assert kline.search_instruments(_request(repo), "SOXL", 20, "stock")["results"] == []


# ---------------- A 股/ETF 回归: 行为不变 ----------------


def test_daily_stock_path_keeps_enriched_and_live_fill(monkeypatch):
    local = pl.DataFrame({
        "symbol": [STOCK_SYMBOL],
        "date": [dt.date(2026, 10, 12)],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [1.0],
    })
    repo = _Repo(asset_type="stock", daily=local)

    resp = kline.get_daily(_request(repo), STOCK_SYMBOL, 120, "2026-10-10", "2026-10-12", None)

    assert resp["source"] == "enriched"
    assert resp["rows"] == local.to_dicts()

    # 本地空 → 仍走 A 股实时补拉路径 (crypto 分支未泄漏)
    filled: list[str] = []
    monkeypatch.setattr(kline, "_compute_live_daily_rows", lambda *a, **k: filled.append("fill") or [])
    fallback = kline.get_daily(_request(_Repo(asset_type="stock")), STOCK_SYMBOL, 120, "2026-10-10", "2026-10-12", None)

    assert filled == ["fill"]
    assert fallback["rows"] == []
    assert "source" not in fallback


def test_minute_stock_path_keeps_240_bars_and_price_limit(monkeypatch):
    bars = _minute_rows(datetime(2026, 10, 12, 9, 31), 240, symbol=STOCK_SYMBOL)
    repo = _Repo(asset_type="stock", minute_by_day={dt.date(2026, 10, 12): bars})
    monkeypatch.setattr(kline, "cn_today", lambda: dt.date(2026, 10, 15))
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 10, 15, 10, 0, tzinfo=CN_TZ))
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", _forbidden("本地完整不应补拉"))
    monkeypatch.setattr(
        kline, "_get_previous_closes", lambda repo, symbol, dates, asset_type: {day: 9.9 for day in dates}
    )

    resp = kline.get_minute(_request(repo, capabilities=SimpleNamespace()), STOCK_SYMBOL, dt.date(2026, 10, 12), live=False)

    assert resp["source"] == "local"
    assert resp["asset_type"] == "stock"
    assert len(resp["rows"]) == 240
    assert resp["prev_close"] == 9.9
    assert resp["price_limit"] == {
        "rate": 0.10, "limit_up": None, "limit_down": None, "no_limit": False, "source": "rule",
    }
    # crypto 分支的字段不出现在 A 股响应里
    assert "expected_bars" not in resp and "is_complete" not in resp
    assert repo.minute_calls == [(STOCK_SYMBOL, dt.date(2026, 10, 12), "stock")]


def test_minute_stock_incomplete_returns_local_partial(monkeypatch):
    """上游口径: 历史日期本地有残缺数据时直接回 local_partial, 不去问一个给不出
    历史分钟的源; 用户点「尝试补齐」走 sync/minute 落盘后再读 (见前端横幅)。
    这里守住 A 股路径本身: 残缺数据必须如实带 expected/actual, 不能被当完整。"""
    local = _minute_rows(datetime(2026, 10, 12, 9, 31), 3, symbol=STOCK_SYMBOL)
    repo = _Repo(asset_type="stock", minute_by_day={dt.date(2026, 10, 12): local})
    monkeypatch.setattr(kline, "cn_today", lambda: dt.date(2026, 10, 15))
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 10, 15, 10, 0, tzinfo=CN_TZ))
    live = MagicMock(return_value=_minute_rows(datetime(2026, 10, 12, 9, 31), 5, symbol=STOCK_SYMBOL))
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", live)

    resp = kline.get_minute(_request(repo, capabilities=SimpleNamespace()), STOCK_SYMBOL, dt.date(2026, 10, 12), live=False)

    assert resp["source"] == "local_partial"
    assert resp["expected"] == 240
    assert resp["actual"] == len(resp["rows"]) == 3
    live.assert_not_called()
