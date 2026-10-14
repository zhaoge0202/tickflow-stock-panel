from __future__ import annotations

import threading
from datetime import date
from types import SimpleNamespace

import polars as pl

from app.indicators.pipeline import _select_storage_cols
from app.services.quote_service import QuoteService
from app.tickflow.repository import KlineRepository


def test_build_daily_keeps_last_quote_for_duplicate_symbol():
    df = QuoteService._build_daily([
        {
            "symbol": "002491.SZ",
            "last_price": 12.1,
            "open": 12.0,
            "high": 12.2,
            "low": 11.9,
            "volume": 1,
            "amount": 100,
        },
        {
            "symbol": "002491.SZ",
            "last_price": 12.5,
            "open": 12.3,
            "high": 12.6,
            "low": 12.2,
            "volume": 2,
            "amount": 200,
        },
    ])

    assert df.height == 1
    assert df["symbol"][0] == "002491.SZ"
    assert df["close"][0] == 12.5
    assert df["volume"][0] == 2


def test_repository_dedupe_symbol_date_keeps_last_row():
    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ", "B.SZ"],
        "date": [date(2026, 7, 7), date(2026, 7, 7), date(2026, 7, 7)],
        "close": [1.0, 2.0, 3.0],
    })

    deduped = KlineRepository._dedupe_symbol_date(df, "test")

    assert deduped.height == 2
    rows = {row["symbol"]: row["close"] for row in deduped.to_dicts()}
    assert rows == {"A.SZ": 2.0, "B.SZ": 3.0}


def test_repository_collect_unique_symbol_date_dedupes_before_materialization(tmp_path):
    part_a = tmp_path / "part-a.parquet"
    part_b = tmp_path / "part-b.parquet"
    common = {
        "symbol": ["A.SZ", "B.SZ"],
        "date": [date(2026, 7, 7), date(2026, 7, 7)],
        "close": [1.0, 3.0],
    }
    pl.DataFrame(common).write_parquet(part_a)
    pl.DataFrame(common).write_parquet(part_b)

    out = KlineRepository._collect_unique_symbol_date(
        pl.scan_parquet(str(tmp_path / "*.parquet"))
    )

    assert out.height == 2
    assert out.select(["symbol", "date"]).is_duplicated().sum() == 0

    batched = KlineRepository._collect_unique_symbol_date_batched(
        pl.scan_parquet(str(tmp_path / "*.parquet")),
        ["A.SZ", "B.SZ"],
        batch_size=1,
    )
    assert batched.to_dicts() == out.to_dicts()


def test_repository_enriched_range_pushes_down_columns_and_symbols(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    for ds, closes in (("2026-07-06", [1.0, 3.0]), ("2026-07-07", [2.0, 4.0])):
        target = base / f"date={ds}"
        target.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["A.SZ", "B.SZ"],
            "date": [date.fromisoformat(ds)] * 2,
            "close": closes,
            "volume": [10, 20],
        }).write_parquet(target / "part.parquet")

    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_glob = str(base / "**" / "*.parquet")

    out = repo.get_enriched_range(
        date(2026, 7, 6),
        date(2026, 7, 7),
        symbols=["A.SZ"],
        columns=["close"],
    )

    assert out is not None
    assert out.columns == ["symbol", "date", "close"]
    assert out["symbol"].to_list() == ["A.SZ", "A.SZ"]
    assert repo.get_enriched_range(
        date(2026, 7, 6),
        date(2026, 7, 7),
        columns=["change_pct"],
    ) is None


def test_enriched_storage_paths_keep_unique_symbol_date(tmp_path):
    duplicated = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ", "B.SZ"],
        "date": [date(2026, 7, 7)] * 3,
        "close": [1.0, 2.0, 3.0],
    })

    selected = _select_storage_cols(duplicated)
    assert selected.height == 2
    assert dict(zip(selected["symbol"], selected["close"], strict=True))["A.SZ"] == 2.0

    repo = KlineRepository.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    repo._write_lock = threading.Lock()
    repo._write_daily_partition(duplicated, "kline_daily_enriched")

    stored = pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-07" / "part.parquet"
    )
    assert stored.height == 2
    assert stored.select(["symbol", "date"]).is_duplicated().sum() == 0


def test_repository_ensure_date_column_for_partition_file():
    df = pl.DataFrame({
        "symbol": ["A.SZ"],
        "close": [2.0],
    })

    out = KlineRepository._ensure_date_column(df, date(2026, 7, 10))

    assert "date" in out.columns
    assert out["date"][0] == date(2026, 7, 10)


def test_quote_service_shutdown_stop_preserves_realtime_preference(monkeypatch):
    saved: list[bool] = []
    monkeypatch.setattr(
        QuoteService,
        "_save_enabled",
        staticmethod(lambda enabled: saved.append(enabled)),
    )

    qs = QuoteService()
    qs.stop(persist_enabled=False)

    assert saved == []

    qs.disable()

    assert saved == [False]
