"""OKX 永续 provider 字段映射与口径测试 (CONTRIBUTING §3.2 / §3.3 / §4)。

口径: 日归属 = 北京自然日 (OKX 官方日线以 UTC+8 零点切日); 分钟 datetime = 北京墙钟
naive; volume/amount 保持上游口径不换算; 无复权/无涨跌停/无股本。
契约: 维表解析不出标的必须上抛, 单标的取数失败软跳过 (不得静默留空维表)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.data_providers.okx.client import OkxClientError
from app.data_providers.okx.provider import (
    OkxProvider,
    okx_ms_to_cn_wallclock,
    okx_ms_to_contract_date,
)

SYMBOL = "SOXL-USDT-SWAP"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _bar(ts: datetime, *, o=10.0, h=11.0, low=9.5, c=10.5, vol=1234.5, quote=12962.25, confirm="1") -> list:
    return [_ms(ts), str(o), str(h), str(low), str(c), str(vol), str(vol), str(quote), confirm]


class FakeClient:
    """最小假客户端: 只实现 provider 真正调用的方法。"""

    def __init__(self, instruments=None, candles=None, history=None) -> None:
        self._instruments = instruments if instruments is not None else []
        self._candles = candles or {}
        self._history = history or {}
        self.calls: list[tuple] = []

    def instruments(self, inst_type="SWAP"):
        self.calls.append(("instruments", inst_type))
        return self._instruments

    def candles(self, inst_id, bar="1D", limit=100):
        self.calls.append(("candles", inst_id, bar, limit))
        if inst_id not in self._candles:
            raise OkxClientError(f"no data for {inst_id}")
        return self._candles[inst_id]

    def history_candles(self, inst_id, bar, after_ms, limit=100):
        self.calls.append(("history", inst_id, bar, after_ms, limit))
        return self._history.get(inst_id, [])

    @staticmethod
    def sleep_for_rate_limit(seconds=0.12):
        return None


# ---------------- 时间换算 ----------------


def test_okx_ms_maps_to_beijing_wallclock_and_beijing_day():
    # UTC 2026-10-12 16:00 = 北京 2026-10-13 00:00 → 日归属 10-13 (OKX 日线切换点)
    boundary = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
    assert okx_ms_to_cn_wallclock(_ms(boundary)) == datetime(2026, 10, 13, 0, 0)
    assert okx_ms_to_contract_date(_ms(boundary)) == date(2026, 10, 13)
    # 北京时间 23:59 = UTC 15:59 → 仍是 10-13
    late = datetime(2026, 10, 13, 15, 59, tzinfo=UTC)
    assert okx_ms_to_cn_wallclock(_ms(late)) == datetime(2026, 10, 13, 23, 59)
    assert okx_ms_to_contract_date(_ms(late)) == date(2026, 10, 13)


# ---------------- 维表 ----------------


def test_instruments_keep_only_usdt_linear_live_swaps():
    rows = [
        {"instId": SYMBOL, "instFamily": "SOXL-USDT", "settleCcy": "USDT",
         "ctType": "linear", "state": "live", "instType": "SWAP"},
        # 币本位 / 交割 / 已下线 一律剔除
        {"instId": "BTC-USD-SWAP", "settleCcy": "BTC", "ctType": "inverse",
         "state": "live", "instType": "SWAP"},
        {"instId": "OLD-USDT-SWAP", "settleCcy": "USDT", "ctType": "linear",
         "state": "suspend", "instType": "SWAP"},
        {"instId": "FUT-USDT-2601", "settleCcy": "USDT", "ctType": "linear",
         "state": "live", "instType": "FUTURES"},
    ]
    out = OkxProvider(FakeClient(instruments=rows)).get_crypto_instruments()
    assert [r["symbol"] for r in out] == [SYMBOL]
    assert out[0]["asset_type"] == "crypto"
    assert out[0]["exchange"] == "OKX"
    # symbol 保持上游 instId 原样 (含连字符), 不套 A 股后缀规则
    assert "." not in out[0]["symbol"]


def test_empty_instruments_raises_instead_of_silent_empty():
    with pytest.raises(OkxClientError):
        OkxProvider(FakeClient(instruments=[])).get_crypto_instruments()


# ---------------- 日K ----------------


def test_daily_rows_use_beijing_day_and_passthrough_units():
    client = FakeClient(candles={SYMBOL: [
        _bar(datetime(2026, 10, 12, 16, 0, tzinfo=UTC), c=10.5, vol=1234.5, quote=12962.25),
        _bar(datetime(2026, 10, 11, 16, 0, tzinfo=UTC), c=10.0, vol=100.0, quote=1000.0),
    ]})
    df = OkxProvider(client).get_daily([SYMBOL], start_date=date(2026, 10, 10), end_date=date(2026, 10, 13))
    # 新→旧入参, 出参必须按日期升序且归到北京日
    assert df["date"].to_list() == [date(2026, 10, 12), date(2026, 10, 13)]
    # 单位透传: volume 不是「手」, 不得放大或缩小 100 倍
    assert sorted(df["volume"].to_list()) == [100.0, 1234.5]
    assert sorted(df["amount"].to_list()) == [1000.0, 12962.25]


def test_daily_single_symbol_failure_is_soft():
    client = FakeClient(candles={SYMBOL: [_bar(datetime(2026, 10, 12, 16, 0, tzinfo=UTC))]})
    # 缺数据的标的抛错 → 只跳过它, 不影响同批其他标的
    df = OkxProvider(client).get_daily([SYMBOL, "BTC-USDT-SWAP"])
    assert df["symbol"].to_list() == [SYMBOL]


def test_daily_pages_history_when_window_is_older_than_recent_bars():
    recent = [_bar(datetime(2026, 10, 12, 16, 0, tzinfo=UTC), confirm="0")]
    older = [_bar(datetime(2026, 10, 11, 16, 0, tzinfo=UTC))]
    client = FakeClient(candles={SYMBOL: recent}, history={SYMBOL: older})
    df = OkxProvider(client).get_daily([SYMBOL], start_date=date(2026, 10, 11), end_date=date(2026, 10, 13))
    # 窗口早于近端首根时必须翻页回补, 否则会静默少一段历史
    assert df["date"].to_list() == [date(2026, 10, 12), date(2026, 10, 13)]
    assert any(call[0] == "history" for call in client.calls)


def test_daily_without_start_date_does_not_page():
    client = FakeClient(candles={SYMBOL: [_bar(datetime(2026, 10, 12, 16, 0, tzinfo=UTC))]})
    OkxProvider(client).get_daily([SYMBOL])
    assert not any(call[0] == "history" for call in client.calls)


# ---------------- 分钟K ----------------


def test_minute_datetime_is_cn_wallclock():
    # UTC 2026-10-13 01:30 = 北京 09:30
    client = FakeClient(candles={SYMBOL: [_bar(datetime(2026, 10, 13, 1, 30, tzinfo=UTC), vol=7.5, quote=78.0)]})
    df = OkxProvider(client).get_minute([SYMBOL])
    assert df["datetime"].to_list() == [datetime(2026, 10, 13, 9, 30)]
    assert df["volume"].to_list() == [7.5]


def test_minute_window_is_interpreted_as_cn_wallclock():
    client = FakeClient(candles={SYMBOL: []})
    OkxProvider(client).get_minute(
        [SYMBOL],
        start_time=datetime(2026, 10, 13, 0, 0),
        end_time=datetime(2026, 10, 14, 0, 0),
    )
    call = next(c for c in client.calls if c[0] == "candles")
    assert call[2] == "1m"


def test_minute_drops_bars_newer_than_requested_end():
    client = FakeClient(candles={SYMBOL: [
        _bar(datetime(2026, 10, 13, 2, 0, tzinfo=UTC)),   # 北京 10:00, 超出窗口
        _bar(datetime(2026, 10, 13, 1, 30, tzinfo=UTC)),  # 北京 09:30
    ]})
    df = OkxProvider(client).get_minute([SYMBOL], end_time=datetime(2026, 10, 13, 9, 30))
    assert df["datetime"].to_list() == [datetime(2026, 10, 13, 9, 30)]


# ---------------- 实时快照 ----------------


def test_realtime_prev_close_comes_from_last_closed_daily_bar():
    # 第一根未完成 (confirm=0), 其后一根才是上一合约日收盘
    client = FakeClient(candles={SYMBOL: [
        _bar(datetime(2026, 10, 12, 16, 0, tzinfo=UTC), c=10.5, confirm="0"),
        _bar(datetime(2026, 10, 11, 16, 0, tzinfo=UTC), c=10.0, confirm="1"),
    ]})
    rows = OkxProvider(client).get_realtime([SYMBOL])
    assert len(rows) == 1
    assert rows[0]["prev_close"] == 10.0
    assert rows[0]["last_price"] == 10.5
    # 入口契约: change_pct 为小数制
    assert rows[0]["change_pct"] == pytest.approx((10.5 - 10.0) / 10.0)


def test_realtime_without_symbols_returns_empty():
    assert OkxProvider(FakeClient()).get_realtime([]) == []


def test_realtime_single_symbol_failure_is_soft():
    client = FakeClient(candles={SYMBOL: [_bar(datetime(2026, 10, 12, 16, 0, tzinfo=UTC))]})
    rows = OkxProvider(client).get_realtime([SYMBOL, "BTC-USDT-SWAP"])
    assert [r["symbol"] for r in rows] == [SYMBOL]


# ---------------- 契约: 空帧列形状 ----------------


def test_empty_daily_returns_empty_frame_not_raise():
    client = FakeClient(candles={SYMBOL: []})
    df = OkxProvider(client).get_daily([SYMBOL])
    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()


# ---------------- 代理配置 ----------------

def test_client_reads_dedicated_proxy_env_only(monkeypatch):
    """代理只认 crypto 专用变量: 通用 HTTPS_PROXY 不得被 crypto 源独占使用。"""
    from app.data_providers.okx.client import PROXY_ENV, OkxClient

    monkeypatch.delenv(PROXY_ENV, raising=False)
    assert OkxClient().proxy is None
    assert "proxy" not in OkxClient()._client_kwargs()

    monkeypatch.setenv(PROXY_ENV, "http://127.0.0.1:10808")
    assert OkxClient().proxy == "http://127.0.0.1:10808"
    assert OkxClient()._client_kwargs()["proxy"] == "http://127.0.0.1:10808"

    # 显式入参优先于环境变量 (测试/多环境)
    assert OkxClient(proxy="http://127.0.0.1:9999").proxy == "http://127.0.0.1:9999"

    # 空白值视为未配置, 不做成空串代理
    monkeypatch.setenv(PROXY_ENV, "   ")
    assert OkxClient().proxy is None
