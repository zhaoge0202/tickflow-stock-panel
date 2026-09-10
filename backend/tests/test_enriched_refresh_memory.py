from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.indicators import pipeline


@pytest.fixture
def history(monkeypatch):
    monkeypatch.setattr(pipeline, "_custom_signal_exprs", {
        "signal_test_previous": (pl.col("close") > pl.col("close").shift(1).over("symbol")),
    })
    symbols = ["600000.SH", "300001.SZ", "688001.SH", "000001.SZ", "920001.BJ"]
    rows = []
    for i, symbol in enumerate(symbols):
        for day in range(160):
            close = 10 + i + day * 0.01 + (day % 7) * 0.2
            rows.append({
                "symbol": symbol, "date": date(2025, 1, 1) + timedelta(days=day),
                "open": close - 0.1, "high": close + 0.3, "low": close - 0.3,
                "close": close, "volume": float(1000 + day * 7), "amount": close * 1000,
                "raw_close": close, "raw_high": close + 0.3, "raw_low": close - 0.3,
            })
    return pl.DataFrame(rows).sample(fraction=1, shuffle=True, seed=17)


@pytest.mark.parametrize("with_shares", [False, True])
def test_history_metadata_matches_original_refresh(history, tmp_path, with_shares):
    symbols = history["symbol"].unique().sort().to_list()
    instruments = pl.DataFrame({
        "symbol": symbols[:-1], "name": ["stock", "ST stock", "stock", "stock"],
        "total_shares": [2000000.0] * 4, "float_shares": [1000000.0] * 4,
    })
    shares = pl.DataFrame({
        "symbol": [symbols[0]], "period_end": [date(2025, 2, 1)],
        "announce_date": [date(2025, 3, 1)], "float_shares": [500000.0],
    }) if with_shares else None
    benchmark = history.filter(pl.col("symbol") == symbols[0]).with_columns(
        pl.lit("000001.SH").alias("symbol"),
    )
    index_path = tmp_path / "kline_index_daily" / "part.parquet"
    index_path.parent.mkdir()
    benchmark.write_parquet(index_path)
    expected = pipeline.compute_limit_signals(
        pipeline.compute_signals(pipeline.attach_deviation_columns(
            pipeline.compute_indicators(history.sort("symbol", "date")), tmp_path,
        )),
        instruments, historical_shares=shares,
    )
    missing = [c for c in ("name", "total_shares", "float_shares") if c not in expected.columns]
    expected = expected.join(instruments.select("symbol", *missing), on="symbol", how="left")
    actual = pipeline.compute_enriched_history_window(
        history, tmp_path, instruments=instruments, historical_shares=shares, sym_batch=2,
        include_instrument_metadata=True,
    )
    assert_frame_equal(actual, expected.sort("symbol", "date"), check_exact=True)


@pytest.mark.parametrize("instruments", [None, pl.DataFrame(), pl.DataFrame({"symbol": ["600000.SH"]})])
def test_history_optional_metadata_keeps_legacy_inputs(history, tmp_path, instruments):
    expected = pipeline.compute_enriched_history_window(history, tmp_path, instruments, sym_batch=2)
    actual = pipeline.compute_enriched_history_window(
        history, tmp_path, instruments, sym_batch=2, include_instrument_metadata=True,
    )
    assert_frame_equal(actual, expected, check_exact=True)


def test_history_wide_sort_is_bounded_by_batch(history, tmp_path, monkeypatch):
    original_sort = pl.DataFrame.sort

    def bounded_sort(frame, *args, **kwargs):
        if frame.width > history.width:
            assert frame["symbol"].n_unique() <= 2, "full history wide sort copies the cache"
        return original_sort(frame, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "sort", bounded_sort)
    result = pipeline.compute_enriched_history_window(history, tmp_path, sym_batch=2)
    assert result.select("symbol", "date").equals(history.select("symbol", "date").sort("symbol", "date"))
