"""自定义分钟数据源路由回归测试。

对应设计文档 §4 测试矩阵 (docs/superpowers/specs/2026-07-18-minute-provider-unification-design.md)。

覆盖三个阻断问题:
1. stock-sdk 默认 freq 漂移 (5m → 1m)
2. 自定义源异常直接 500 (无 try/except)
3. 插件化路由重复 + asset_type 未透传

mock 范式沿用 test_stocksdk_provider.py (monkeypatch 模块属性)。
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import httpx
import polars as pl

from app.plugins.stocksdk import provider as sp
from app.plugins.stocksdk.provider import StockSDKProvider
from app.services import kline_sync


# ---------- 辅助 ----------

def _mock_minute_df(symbol: str = "600519.SH") -> pl.DataFrame:
    """构造非空分钟 K df, 用于 mock provider.get_minute 返回值。"""
    return pl.DataFrame({
        "symbol": [symbol],
        "datetime": [datetime(2026, 1, 15, 9, 35, 0)],
        "open": [100.0],
        "high": [101.0],
        "low": [99.5],
        "close": [100.5],
        "volume": [1000.0],
        "amount": [100500.0],
    })


def _setup_custom_provider(monkeypatch, provider: object, has_dataset: bool = True) -> None:
    """统一 mock 自定义分钟源路由前置: preferences + provider_has_dataset + get_provider。

    - preferences.get_minute_data_provider → "mock_src"
    - custom.provider_has_dataset → has_dataset
    - custom.get_provider → provider
    """
    monkeypatch.setattr(
        kline_sync.preferences,
        "get_minute_data_provider",
        lambda: "mock_src",
    )
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: has_dataset,
    )
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider",
        lambda name: provider,
    )


# ---------- 测试 1: 自定义源成功返回 1 分钟 K ----------

def test_custom_minute_provider_returns_1m_k(monkeypatch):
    """§4 测试 1: 自定义源成功返回 1m K, 且 provider 收到 freq="1m"。"""
    spy = MagicMock(return_value=_mock_minute_df())
    mock_provider = MagicMock()
    mock_provider.get_minute = spy
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"],
        datetime(2026, 1, 15, 9, 25, 0),
        datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
    )

    assert fallback is False
    assert df is not None
    assert not df.is_empty()
    # spy 收到 freq="1m" 和 asset_type="stock"
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("freq") == "1m"
    assert kwargs.get("asset_type") == "stock"


# ---------- 测试 2: stock-sdk 收到 freq=1m → bridge job period="1" ----------

def test_stocksdk_get_minute_receives_freq_1m(monkeypatch):
    """§4 测试 2: StockSDKProvider.get_minute(freq="1m") → bridge job period == "1"。

    bridge.mjs opMinute 用 String(period), 1m → "1"。
    """
    captured: dict = {}

    def fake_run_job(job, timeout=None):
        captured["job"] = job
        # 返回空结果, 测试只验证 job.period
        return {"ok": True, "op": job["op"], "rows": {}}

    monkeypatch.setattr(sp.bridge, "run_job", fake_run_job)

    StockSDKProvider().get_minute(
        ["600519.SH"], None, None, freq="1m",
    )

    assert captured["job"]["op"] == "minute"
    assert captured["job"]["period"] == "1"


# ---------- 测试 3: 自定义源异常 + TickFlow 也失败 → 返回空 (非 500) ----------

def test_custom_provider_exception_no_500(monkeypatch):
    """§4 测试 3: 自定义源抛异常 + TickFlow 也失败,
    fetch_minute_single / sync_minute_batch 返回空 df。
    """
    # 自定义源抛异常
    mock_provider = MagicMock()
    mock_provider.get_minute.side_effect = httpx.TimeoutException("timeout")
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # mock get_client 返回 mock client, 其 klines.batch raise (TickFlow 也失败)
    mock_tf = MagicMock()
    mock_tf.klines.batch.side_effect = Exception("tickflow fail")
    monkeypatch.setattr(kline_sync, "get_client", lambda: mock_tf)

    # fetch_minute_single: 自定义源异常 → fall through → TickFlow 异常 → 返回空
    df_single = kline_sync.fetch_minute_single(
        "600519.SH", date(2026, 1, 15), asset_type="stock",
    )
    assert isinstance(df_single, pl.DataFrame)
    assert df_single.is_empty()

    # sync_minute_batch: 同一路径, 返回空
    df_batch = kline_sync.sync_minute_batch(
        ["600519.SH"],
        start_time=datetime(2026, 1, 15, 9, 25, 0),
        end_time=datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
    )
    assert isinstance(df_batch, pl.DataFrame)
    assert df_batch.is_empty()


# ---------- 测试 4: 未配 minute dataset → 回退 TickFlow ----------

def test_provider_without_minute_dataset_fallback(monkeypatch):
    """§4 测试 4: provider_has_dataset 返回 False → (None, True) 回退 TickFlow。"""
    mock_provider = MagicMock()
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=False)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], None, None, asset_type="stock",
    )

    assert fallback is True
    assert df is None
    # provider.get_minute 不应被调用 (回退决策在前)
    mock_provider.get_minute.assert_not_called()


# ---------- 测试 5: asset_type 透传到 provider ----------

def test_asset_type_threaded_to_provider(monkeypatch):
    """§4 测试 5: stock/etf/index asset_type 透传到 provider.get_minute。"""
    spy = MagicMock(return_value=_mock_minute_df())
    mock_provider = MagicMock()
    mock_provider.get_minute = spy
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # 三次调用不同 asset_type
    kline_sync.fetch_minute_single("600519.SH", date(2026, 1, 15), asset_type="stock")
    kline_sync.fetch_minute_single("510300.SH", date(2026, 1, 15), asset_type="etf")
    kline_sync.fetch_minute_single("000001.SH", date(2026, 1, 15), asset_type="index")

    # spy 被调 3 次, 每次收到对应 asset_type
    assert spy.call_count == 3
    received_assets = [call.kwargs.get("asset_type") for call in spy.call_args_list]
    assert received_assets == ["stock", "etf", "index"]


# ---------- 测试 6: 自定义源成功时不调 TickFlow ----------

def test_custom_success_skips_tickflow(monkeypatch):
    """§4 测试 6: fetch_minute_single 自定义源成功 → 不调 get_client。"""
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    # get_client 设为 spy, 若被调说明路由失败
    get_client_spy = MagicMock(name="get_client_spy")
    monkeypatch.setattr(kline_sync, "get_client", get_client_spy)

    df = kline_sync.fetch_minute_single(
        "600519.SH", date(2026, 1, 15), asset_type="stock",
    )

    # 返回的是 mock provider 的 df
    assert df is expected_df
    # TickFlow 路径未进入
    get_client_spy.assert_not_called()


# ---------- 测试 7: sync_minute_batch 自定义源成功直接返回 ----------

def test_sync_minute_batch_custom_success_returns_directly(monkeypatch):
    """§4 测试 7: sync_minute_batch 自定义源成功 → 直接 return, 不走 segment 逻辑。"""
    expected_df = _mock_minute_df()
    mock_provider = MagicMock()
    mock_provider.get_minute.return_value = expected_df
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    get_client_spy = MagicMock(name="get_client_spy")
    monkeypatch.setattr(kline_sync, "get_client", get_client_spy)

    df = kline_sync.sync_minute_batch(
        ["600519.SH"],
        start_time=datetime(2026, 1, 15, 9, 25, 0),
        end_time=datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
    )

    # 返回 mock provider 的 df, 不走 segment
    assert df is expected_df
    get_client_spy.assert_not_called()


# ---------- 测试 8: on_chunk_done 包装 (2参 → 3参补 seg_label='custom') ----------

def test_on_chunk_done_wrapped_to_3_args(monkeypatch):
    """on_chunk_done 包装: provider 内部以 2 参 (cur, total) 调用 →
    上层 3 参 (cur, total, seg_label) spy 收到 seg_label='custom'。

    设计文档 §2: 保证自定义源路径进度展示不降级 (与 TickFlow 路径 3 参回调对齐)。
    """
    upper_cb = MagicMock(name="upper_3arg_cb")

    def provider_get_minute_side_effect(symbols, *, start_time, end_time,
                                        asset_type, freq, on_chunk_done):
        # 模拟 provider 实现内部以 2 参调用 on_chunk_done
        # (如 GenericHTTPProvider/provider.py:127 / StockSDKProvider/provider.py:166)
        if on_chunk_done is not None:
            on_chunk_done(1, 3)
        return _mock_minute_df()

    mock_provider = MagicMock()
    mock_provider.get_minute.side_effect = provider_get_minute_side_effect
    _setup_custom_provider(monkeypatch, mock_provider, has_dataset=True)

    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"],
        datetime(2026, 1, 15, 9, 25, 0),
        datetime(2026, 1, 15, 15, 5, 0),
        asset_type="stock",
        on_chunk_done=upper_cb,
    )

    assert fallback is False
    assert df is not None
    # 上层 3 参 spy 被调用一次, 收到 (1, 3, "custom")
    upper_cb.assert_called_once_with(1, 3, "custom")


# ---------- 测试 9: get_minute_batch 按 asset_type 拆分调用 sync_minute_batch ----------

def test_get_minute_batch_splits_stock_and_etf(monkeypatch):
    """get_minute_batch 把 incomplete 拆成 stock/ETF 两组, 分别以
    asset_type='stock'/'etf' 调用 sync_minute_batch, 结果 concat 返回。

    覆盖 kline.py get_minute_batch 的双调用拼接逻辑 (本次提交改动量最大的部分)。
    契约: 本端点只接受 stock/ETF (指数走 /api/index/minute), 故两分支覆盖全部 incomplete。
    """
    from app.api import kline as kline_api

    # mock sync_minute_batch: stock 返回 df_s, etf 返回 df_e (不同 symbol 便于 concat 后 filter 验证)
    def fake_sync(symbols, *, start_time, end_time, batch_size, rpm, asset_type):
        if asset_type == "stock":
            return _mock_minute_df(symbol="600519.SH")
        if asset_type == "etf":
            return _mock_minute_df(symbol="510300.SH")
        return pl.DataFrame()
    sync_spy = MagicMock(side_effect=fake_sync)
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync_spy)

    # mock repo: ETF 集合含 510300.SH; 本地分钟K返回空 (强制走 incomplete 补拉)
    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = {"510300.SH"}
    mock_repo.get_minute_batch.return_value = pl.DataFrame()

    # mock capset: 有权限, limits 返回 None (lim.batch 访问被 `if lim else` 守护)
    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset

    body = {"symbols": ["600519.SH", "510300.SH"], "date": "2026-01-15"}
    result = kline_api.get_minute_batch(mock_request, body)

    # sync_minute_batch 被调 2 次, asset_type 分别为 stock 和 etf
    assert sync_spy.call_count == 2
    call_assets = sorted(call.kwargs.get("asset_type") for call in sync_spy.call_args_list)
    assert call_assets == ["etf", "stock"]

    # 两个 symbol 都在结果里 (concat 后按 symbol filter 命中)
    assert "600519.SH" in result["data"]
    assert "510300.SH" in result["data"]
