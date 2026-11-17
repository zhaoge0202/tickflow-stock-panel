from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.services import quote_service

TODAY = date(2026, 10, 29)


@pytest.fixture(params=["etf", "index"])
def sample(tmp_path, monkeypatch, request):
    asset_type = request.param
    monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY)
    days = [TODAY - timedelta(days=i) for i in range(1, 145)]
    days = sorted(d for d in days if d.weekday() < 5)
    for i, day in enumerate(days):
        frame = pl.DataFrame({
            "symbol": ["510300.SH", "159915.SZ"], "date": [day] * 2,
            "open": [10.0, 20.0], "high": [11.0+i/100, 21.0+i/100],
            "low": [9.0, 19.0], "close": [10.0+i/100, 20.0+i/100],
            "volume": [10000.0, 20000.0], "amount": [100000.0, 400000.0],
        })
        path = tmp_path / f"kline_{asset_type}_daily" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        frame.write_parquet(path)
    today = frame.with_columns(pl.lit(TODAY).alias("date"))
    output = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest_asset=lambda _: (today, TODAY),
        flush_live_enriched_asset=lambda asset, df: output.append(df),
        merge_live_enriched_asset=lambda asset, df: output.append(df),
    )
    service = quote_service.QuoteService()
    service.set_repo(repo)
    return service, repo, today, output, days, asset_type


def test_warm_etf_updates_do_not_rescan_history(sample, monkeypatch):
    service, _repo, today, output, _, asset_type = sample
    original = quote_service.guarded_collect
    scans = []

    def collect(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(quote_service, "guarded_collect", collect)
    service._flush_live_enriched(today, asset_type=asset_type)
    service._flush_live_enriched(today, asset_type=asset_type, merge=True)
    assert len(output) == 2
    assert_frame_equal(output[0], output[1], check_exact=True)
    assert len(scans) == 1


@pytest.mark.parametrize("change", ["history", "factor", "rollover", "new_symbol", "delete"])
def test_warm_etf_output_matches_cold_recompute_after_changes(sample, monkeypatch, change):
    service, repo, today, output, days, asset_type = sample
    if asset_type == "index" and change == "factor":
        pytest.skip("指数不使用复权因子")
    service._flush_live_enriched(today, asset_type=asset_type)
    root = repo.store.data_dir
    if change == "history":
        path = root / f"kline_{asset_type}_daily" / f"date={days[-1]}" / "part.parquet"
        pl.read_parquet(path).with_columns(pl.lit(15.0).alias("close")).write_parquet(path)
    elif change == "delete":
        (root / f"kline_{asset_type}_daily" / f"date={days[-1]}" / "part.parquet").unlink()
    elif change == "factor":
        path = root / "adj_factor_etf/all.parquet"
        path.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["510300.SH"], "trade_date": [days[-5]], "ex_factor": [1.1]}).write_parquet(path)
    elif change == "rollover":
        path = root / f"kline_{asset_type}_daily" / f"date={TODAY}" / "part.parquet"
        path.parent.mkdir(parents=True)
        today.write_parquet(path)
        monkeypatch.setattr(quote_service, "cn_today", lambda: TODAY + timedelta(days=1))
        today = today.with_columns(pl.lit(TODAY + timedelta(days=1)).alias("date"))
    else:
        today = pl.concat([today, today.head(1).with_columns(pl.lit("510500.SH").alias("symbol"))])
    today = today.with_columns((pl.col("close") + 0.25).alias("close"))
    service._flush_live_enriched(today, asset_type=asset_type)
    cold = quote_service.QuoteService()
    cold.set_repo(repo)
    # Compare with the original uncached glob-read path, including all numeric
    # columns, nulls, adjusted prices and indicator warmup boundaries.
    monkeypatch.setattr(cold, "_live_asset_history", lambda *args: None, raising=False)
    cold._flush_live_enriched(today, asset_type=asset_type)
    assert len(output) == 3
    assert_frame_equal(output[-2], output[-1], check_exact=True)


def test_today_disk_writes_do_not_invalidate_history(sample, monkeypatch):
    service, repo, today, output, _, asset_type = sample
    original = quote_service.guarded_collect
    scans = []

    def collect(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(quote_service, "guarded_collect", collect)
    service._flush_live_enriched(today, asset_type=asset_type)
    path = repo.store.data_dir / f"kline_{asset_type}_daily" / f"date={TODAY}" / "part.parquet"
    path.parent.mkdir(parents=True)
    today = today.with_columns((pl.col("close") + 0.5).alias("close"))
    today.write_parquet(path)
    service._flush_live_enriched(today, asset_type=asset_type)
    assert len(scans) == 1
    assert_frame_equal(output[-1].select("symbol", "raw_close").sort("symbol"),
                       today.select("symbol", pl.col("close").alias("raw_close")).sort("symbol"))


def test_oversized_history_is_not_retained_and_repo_change_clears_cache(sample, monkeypatch):
    service, repo, today, output, _, asset_type = sample
    service._flush_live_enriched(today, asset_type=asset_type)
    assert asset_type in service._live_history_cache
    service.set_repo(repo)
    assert not service._live_history_cache
    monkeypatch.setattr(pl.DataFrame, "estimated_size", lambda *args: 65 * 1024**2)
    service._flush_live_enriched(today, asset_type=asset_type)
    assert not service._live_history_cache
    assert_frame_equal(output[-2], output[-1], check_exact=True)


def test_etf_and_index_history_caches_do_not_evict_or_mix(sample, monkeypatch):
    service, repo, today, _output, days, asset_type = sample
    other_asset = "index" if asset_type == "etf" else "etf"
    root = repo.store.data_dir
    for day in days:
        source = root / f"kline_{asset_type}_daily" / f"date={day}" / "part.parquet"
        target = root / f"kline_{other_asset}_daily" / f"date={day}" / "part.parquet"
        target.parent.mkdir(parents=True)
        pl.read_parquet(source).with_columns((pl.col("close") + 100).alias("close")).write_parquet(target)
    original = quote_service.guarded_collect
    scans = []

    def collect(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(quote_service, "guarded_collect", collect)
    columns = today.columns
    first = service._live_asset_history(asset_type, TODAY, columns)
    other = service._live_asset_history(other_asset, TODAY, columns)
    again = service._live_asset_history(asset_type, TODAY, columns)

    assert len(scans) == 2
    assert again is first
    assert other["close"].min() > first["close"].max()
    assert set(service._live_history_cache) == {"etf", "index"}
