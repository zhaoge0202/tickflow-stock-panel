"""#225/#226 回归: 自定义源分钟K字符串日期解析 + 日K分批失败隔离。"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.loader import GenericHTTPProvider


def _provider(datasets: dict[str, DatasetConfig]) -> GenericHTTPProvider:
    return GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets=datasets,
    ))


def _daily_config(batch: int = 2) -> DatasetConfig:
    return DatasetConfig(
        url="https://example.test/daily",
        field_map={
            "symbol": "symbol", "date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume", "amount": "amount",
        },
        batch=batch,
    )


# ── #225: 分钟K字符串 datetime 不得被 cast 成 null ────────────────

def test_normalize_minute_parses_string_datetime() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * 2,
            # 上游 YAML 映射后仍是字符串; 旧代码直接 cast → 全 null (#225)
            "datetime": ["2026-09-01 09:35:00", "2026-09-01 09:40:00"],
            "close": [10.0, 10.5],
        }
    )
    out = GenericHTTPProvider._normalize_minute(df)
    assert out.schema["datetime"] == pl.Datetime("us")
    assert out["datetime"].null_count() == 0
    assert out["datetime"][0] == datetime(2026, 9, 1, 9, 35)

    # 非法字符串维持 strict=False 宽松行为 (null, 不抛异常)
    bad = pl.DataFrame(
        {"symbol": ["600000.SH"], "datetime": ["not-a-date"], "close": [1.0]}
    )
    out_bad = GenericHTTPProvider._normalize_minute(bad)
    assert out_bad["datetime"].null_count() == 1


def test_normalize_minute_datetime_already_typed_unchanged() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "datetime": [datetime(2026, 9, 1, 9, 35)],
            "close": [10.0],
        }
    )
    out = GenericHTTPProvider._normalize_minute(df)
    assert out["datetime"][0] == datetime(2026, 9, 1, 9, 35)


# ── #226: get_daily 单批失败只隔离该批 ────────────────────────

def _canonical_frame(symbols: list[str], day: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": symbols,
            "date": [date.fromisoformat(day)] * len(symbols),
            "open": [10.0] * len(symbols),
            "high": [11.0] * len(symbols),
            "low": [9.0] * len(symbols),
            "close": [10.5] * len(symbols),
            "volume": [100.0] * len(symbols),
            "amount": [1050.0] * len(symbols),
        }
    )


def test_get_daily_isolates_failed_batch() -> None:
    provider = _provider({"daily": _daily_config(batch=2)})
    calls: list[list[str]] = []

    def request_rows(cfg, symbols=None, **kwargs):
        calls.append(list(symbols))
        if symbols == ["s3", "s4"]:
            raise RuntimeError("502 Bad Gateway")
        return [{"_rows": list(symbols)}]

    provider._request_rows = request_rows
    provider._mapped_frame = lambda cfg, rows: _canonical_frame(
        rows[0]["_rows"], "2026-09-01"
    )

    df = provider.get_daily(
        ["s1", "s2", "s3", "s4", "s5", "s6"],
        datetime(2026, 8, 1), datetime(2026, 9, 1),
    )

    # 3 批都请求过 (失败批重试 1 次后跳过、流程继续), 返回第 1、3 批共 4 行
    assert calls == [["s1", "s2"], ["s3", "s4"], ["s3", "s4"], ["s5", "s6"]]
    assert df.height == 4
    assert set(df["symbol"]) == {"s1", "s2", "s5", "s6"}


def test_get_daily_progress_callback_fires_for_failed_batch() -> None:
    provider = _provider({"daily": _daily_config(batch=2)})

    def request_rows(cfg, symbols=None, **kwargs):
        if symbols == ["s3", "s4"]:
            raise RuntimeError("timeout")
        return [{"_rows": list(symbols)}]

    progress: list[tuple[int, int]] = []
    provider._request_rows = request_rows
    provider._mapped_frame = lambda cfg, rows: _canonical_frame(
        rows[0]["_rows"], "2026-09-01"
    )

    provider.get_daily(
        ["s1", "s2", "s3", "s4"], datetime(2026, 8, 1), datetime(2026, 9, 1),
        on_chunk_done=lambda cur, tot: progress.append((cur, tot)),
    )
    # 失败批也推进进度, 前端进度条不会卡死
    assert progress == [(1, 2), (2, 2)]


def test_get_daily_all_batches_fail_returns_empty() -> None:
    provider = _provider({"daily": _daily_config(batch=2)})

    def request_rows(cfg, symbols=None, **kwargs):
        raise RuntimeError("down")

    provider._request_rows = request_rows
    df = provider.get_daily(["s1", "s2"], datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert df.is_empty()
