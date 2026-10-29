from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.services import screener as screener_module
from app.services.screener import ScreenerService


def _service(tmp_path):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_history=lambda *_args: None,
        get_instruments_asset=lambda _asset_type: pl.DataFrame({"symbol": ["A"]}),
    )
    return ScreenerService(repo)


def test_larger_history_window_is_reused_for_smaller_request(monkeypatch, tmp_path):
    target = date(2026, 1, 10)
    dates = [target - timedelta(days=offset) for offset in range(9, -1, -1)]
    source = pl.DataFrame({
        "symbol": ["A"] * len(dates),
        "date": dates,
        "close": [float(index) for index in range(len(dates))],
    }).lazy()
    calls: list[tuple[date, date]] = []

    monkeypatch.setattr(screener_module, "scan_enriched_parquet", lambda *_args: source)

    def fake_compute(self, history_lf, symbols, *, result_start, result_end):
        calls.append((result_start, result_end))
        frame = history_lf.collect()
        return frame.filter(
            (pl.col("date") >= result_start) & (pl.col("date") <= result_end)
        )

    monkeypatch.setattr(ScreenerService, "_compute_enriched_batches", fake_compute)
    screener_module.ScreenerService.clear_history_cache()
    service = _service(tmp_path)

    larger = service._load_enriched_history(target, 5)
    smaller = service._load_enriched_history(target, 3)

    assert len(calls) == 1
    assert larger.get_column("date").to_list() == dates[-6:]
    assert smaller.get_column("date").to_list() == dates[-4:]


def test_history_cache_does_not_cross_asset_type(monkeypatch, tmp_path):
    target = date(2026, 1, 10)
    source = pl.DataFrame({
        "symbol": ["A", "A"],
        "date": [target - timedelta(days=1), target],
        "close": [1.0, 2.0],
    }).lazy()
    calls: list[str] = []

    monkeypatch.setattr(screener_module, "scan_enriched_parquet", lambda *_args: source)

    def fake_compute(self, history_lf, symbols, *, result_start, result_end):
        calls.append(self.asset_type)
        return history_lf.collect()

    monkeypatch.setattr(ScreenerService, "_compute_enriched_batches", fake_compute)
    screener_module.ScreenerService.clear_history_cache()
    stock = _service(tmp_path)
    etf = ScreenerService(stock.repo, asset_type="etf")

    stock._load_enriched_history(target, 1)
    etf._load_enriched_history(target, 1)

    assert calls == ["stock", "etf"]


def test_smaller_window_computes_floor_once_then_larger_request_reuses(monkeypatch, tmp_path):
    """同日 41/81 两个窗口只应算一次.

    回归点: 缓存键含 lookback 时, run_all 先要 41 日 (12s)、再要 81 日 (30s)
    会各算一遍; 4 个槽位被两个窗口挤满后, 切日期还会反复淘汰重算, 把 API
    线程饿死 (30s 请求超时)。
    """
    target = date(2026, 1, 10)
    dates = [target - timedelta(days=offset) for offset in range(20, 0, -1)]
    source = pl.DataFrame({
        "symbol": ["A"] * len(dates),
        "date": dates,
        "close": [float(index) for index in range(len(dates))],
    }).lazy()
    calls: list[tuple[date, date]] = []

    monkeypatch.setattr(screener_module, "scan_enriched_parquet", lambda *_args: source)

    def fake_compute(self, history_lf, symbols, *, result_start, result_end):
        calls.append((result_start, result_end))
        frame = history_lf.collect()
        return frame.filter(
            (pl.col("date") >= result_start) & (pl.col("date") <= result_end)
        )

    monkeypatch.setattr(ScreenerService, "_compute_enriched_batches", fake_compute)
    screener_module.ScreenerService.clear_history_cache()
    service = _service(tmp_path)

    small = service._load_enriched_history(target, 3)
    large = service._load_enriched_history(target, 15)

    assert len(calls) == 1
    assert small.get_column("date").to_list() == dates[-4:]
    assert large.get_column("date").to_list() == dates[-16:]
