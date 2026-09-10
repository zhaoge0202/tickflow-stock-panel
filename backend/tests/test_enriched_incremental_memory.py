from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.enriched_generation import get_enriched_generation
from app.indicators import pipeline
from app.services import preferences


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_enriched_batch_size", lambda: 2)
    monkeypatch.setattr(pipeline, "_custom_signal_exprs", {})
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda default, rows: 2)
    symbols = [f"{600000 + i}.SH" for i in range(5)]
    rows = []
    for day in range(12):
        for i, symbol in enumerate(symbols):
            close = 10.0 + i + day * 0.1
            rows.append({
                "symbol": symbol, "date": date(2026, 8, 1) + timedelta(days=day),
                "open": close, "high": close + 0.1, "low": close - 0.1,
                "close": close, "volume": 0.0 if i == 4 else 1000.0 + day,
                "amount": 0.0 if i == 4 else 10000.0, "quote_ts": 0,
            })
    raw = pl.DataFrame(rows)
    for frame in raw.partition_by("date"):
        out = tmp_path / "kline_daily" / f"date={frame['date'][0]}" / "part.parquet"
        out.parent.mkdir(parents=True)
        frame.write_parquet(out)
    instruments = pl.DataFrame({
        "symbol": symbols, "name": ["stock"] * 5, "float_shares": [1000000.0] * 5,
    })
    factors = pl.DataFrame({
        "symbol": symbols[:3], "trade_date": [date(2026, 8, 8)] * 3, "ex_factor": [1.1] * 3,
    })
    shares = pl.DataFrame({
        "symbol": symbols[:2], "period_end": [date(2026, 6, 30)] * 2,
        "announce_date": [date(2026, 8, 5)] * 2, "float_shares": [800000.0] * 2,
    })
    for name, frame in (("instruments/all.parquet", instruments), ("adj_factor/all.parquet", factors),
                        ("financials/shares/part.parquet", shares)):
        out = tmp_path / name
        out.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(out)
    return raw, instruments, factors, shares


@pytest.mark.parametrize("adjustment_only", [False, True])
def test_incremental_batches_preserve_values_and_generation(sample, tmp_path, monkeypatch, adjustment_only):
    raw, instruments, factors, shares = sample
    expected = pipeline._select_storage_cols(pipeline.compute_enriched(
        raw, instruments=instruments, factors=factors, historical_shares=shares,
    )).sort("symbol", "date")
    if adjustment_only:
        for frame in raw.partition_by("date"):
            out = tmp_path / "kline_daily_enriched" / f"date={frame['date'][0]}" / "part.parquet"
            out.parent.mkdir(parents=True)
            frame.write_parquet(out)
    monkeypatch.setattr(pipeline, "_load_recent_history", lambda *args, **kwargs: pl.DataFrame())
    original_compute = pipeline.compute_enriched

    def bounded_compute(frame, **kwargs):
        assert frame["symbol"].n_unique() <= 2, "incremental wide compute must be batched"
        return original_compute(frame, **kwargs)

    monkeypatch.setattr(pipeline, "compute_enriched", bounded_compute)
    generation = get_enriched_generation(tmp_path)
    symbols = raw["symbol"].unique().to_list() if adjustment_only else None
    written = pipeline.run_pipeline(tmp_path, symbols=symbols, new_dates_only=True)
    actual = pl.read_parquet(str(tmp_path / "kline_daily_enriched/**/*.parquet"))
    if adjustment_only:
        actual = actual.select(expected.columns)
    assert written > 0
    assert_frame_equal(actual.sort("symbol", "date"), expected, check_exact=True)
    assert get_enriched_generation(tmp_path) != generation


def test_failed_compute_batch_publishes_no_partial_new_dates(sample, tmp_path, monkeypatch):
    original_compute = pipeline.compute_enriched
    calls = 0

    def fail_second(frame, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("batch failed")
        return original_compute(frame, **kwargs)

    monkeypatch.setattr(pipeline, "compute_enriched", fail_second)
    monkeypatch.setattr(pipeline, "_load_recent_history", lambda *args, **kwargs: pl.DataFrame())
    generation = get_enriched_generation(tmp_path)
    with pytest.raises(RuntimeError, match="batch failed"):
        pipeline.run_pipeline(tmp_path, new_dates_only=True)
    assert not list((tmp_path / "kline_daily_enriched").rglob("*.parquet"))
    assert get_enriched_generation(tmp_path) == generation
