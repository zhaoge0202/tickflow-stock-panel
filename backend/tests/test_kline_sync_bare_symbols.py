"""#302: 裸符号日K同步不再静默 0 行。

上游对无交易所后缀的符号返回 200 空 payload, 本地写 0 行且流程"成功"。
修复后: 裸符号按 instruments 维表补全后缀, 无法识别的显式跳过并告警;
批量同步结束统计 0 行标的 (zero_row_out / API zero_row_symbols)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.services import kline_sync
from app.tickflow.repository import DataStore, KlineRepository


def _write_instruments(data_dir, symbols: list[str]) -> None:
    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(out)


def _daily(symbol: str, day: date) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol], "date": [day], "open": [10.0], "high": [11.0],
        "low": [9.0], "close": [10.5], "volume": [100.0], "amount": [1050.0],
    })


class _Provider:
    """自定义源: 只返回 000001.SZ 的数据 (600000 上游 200 空 payload 场景)。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.requested_symbols: list[str] | None = None

    def iter_daily(self, symbols, *args, **kwargs):
        self.requested_symbols = list(symbols)
        yield from self._chunks


def _route_provider(monkeypatch, provider):
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    from app.data_providers import custom
    monkeypatch.setattr(
        custom, "provider_has_dataset", lambda name, dataset: name == "fuyao" and dataset == "daily"
    )
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)


def test_normalize_maps_bare_via_instruments_and_dedups(tmp_path):
    _write_instruments(tmp_path, ["600000.SH", "000001.SZ"])

    out, resolved, skipped = kline_sync._normalize_bare_symbols(
        ["600000", "000001.SZ", "600000.SH"], tmp_path
    )

    assert out == ["600000.SH", "000001.SZ"]
    assert resolved == {"600000": "600000.SH"}
    assert skipped == []


def test_normalize_without_instruments_skips_all_bare(tmp_path, caplog):
    out, resolved, skipped = kline_sync._normalize_bare_symbols(["600000", "000001.SZ"], tmp_path)

    assert out == ["000001.SZ"]
    assert resolved == {}
    assert skipped == ["600000"]
    assert any("维表不可用" in r.message for r in caplog.records)


def test_normalize_skips_ambiguous_bare(tmp_path):
    _write_instruments(tmp_path, ["830001.BJ", "830001.SH", "600000.SH"])

    out, resolved, skipped = kline_sync._normalize_bare_symbols(["830001", "600000"], tmp_path)

    assert out == ["600000.SH"]
    assert resolved == {"600000": "600000.SH"}
    assert skipped == ["830001"]


def test_sync_reports_zero_rows_in_caller_spelling(monkeypatch, tmp_path, caplog):
    """裸符号无维表可补 → 跳过; 带后缀但上游空 payload → 0 行; 均按原始写法回报。"""
    caplog.set_level(logging.WARNING, logger="app.services.kline_sync")
    repo = KlineRepository(DataStore(tmp_path))
    provider = _Provider([_daily("000001.SZ", date(2026, 9, 10))])
    _route_provider(monkeypatch, provider)
    zero: list[str] = []

    written = kline_sync.sync_and_persist_daily_batch(
        ["600000", "000001.SZ", "999999.SZ"], repo, object(),
        start_date=datetime(2026, 9, 1), end_date=datetime(2026, 9, 10),
        zero_row_out=zero,
    )

    assert written == 1
    assert zero == ["999999.SZ", "600000"]  # 0 行标的 + 被跳过的裸符号
    assert any("入库 0 行" in r.message for r in caplog.records)


def test_sync_tickflow_path_normalizes_and_reports_zero(monkeypatch, tmp_path):
    _write_instruments(tmp_path, ["600000.SH"])
    repo = KlineRepository(DataStore(tmp_path))
    captured: dict = {}

    def _fake_sync_daily_batch(symbols, **kwargs):
        captured["symbols"] = list(symbols)
        return pl.DataFrame()  # 上游 200 空 payload → 全部 0 行

    monkeypatch.setattr(kline_sync, "sync_daily_batch", _fake_sync_daily_batch)
    monkeypatch.setattr(
        kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow"
    )
    capset = SimpleNamespace(has=lambda cap: True, limits=lambda cap: None)
    zero: list[str] = []

    written = kline_sync.sync_and_persist_daily_batch(
        ["600000", "000001.SH"], repo, capset,
        start_date=datetime(2026, 9, 1), end_date=datetime(2026, 9, 10),
        zero_row_out=zero,
    )

    assert written == 0
    # 发往上游前已完成规范化: 600000 → 600000.SH
    assert captured["symbols"] == ["600000.SH", "000001.SH"]
    # 回报用调用方原始写法
    assert zero == ["600000", "000001.SH"]


def test_api_sync_batch_returns_zero_row_symbols(monkeypatch, tmp_path):
    from app.api import kline as kline_api

    _write_instruments(tmp_path, ["600000.SH"])
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(
        kline_sync, "sync_daily_batch", lambda symbols, **kwargs: pl.DataFrame()
    )
    monkeypatch.setattr(
        kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow"
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=repo, capabilities=SimpleNamespace(has=lambda cap: True, limits=lambda cap: None),
    )))

    resp = kline_api.sync_batch(request, symbols=["600000", "000001.SH"], days=250)

    assert resp["rows_written"] == 0
    assert resp["zero_row_symbols"] == ["600000", "000001.SH"]
