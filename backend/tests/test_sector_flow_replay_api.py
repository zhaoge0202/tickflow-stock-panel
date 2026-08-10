from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.api import screener as screener_api
from app.services import quote_tick_store

CN = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 8)


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 7, 8, hour, minute, second, tzinfo=CN).timestamp() * 1000)


def _request(tmp_path, *, quote_service=None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo)
    if quote_service is not None:
        state.quote_service = quote_service
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_market_snapshot_replays_historical_quote_ticks(monkeypatch, tmp_path):
    class _ScreenerService:
        def __init__(self, _repo):
            pass

        def latest_date(self):
            return TRADE_DATE

        def _load_enriched_for_date(self, _target):
            return pl.DataFrame([
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "close": 9.8,
                    "change_pct": 0.01,
                    "amount": 80_000,
                    "volume": 8_000,
                    "total_shares": 1_000,
                    "float_shares": 800,
                },
                {
                    "symbol": "000002.SZ",
                    "name": "万科A",
                    "close": 19.5,
                    "change_pct": 0.02,
                    "amount": 70_000,
                    "volume": 3_500,
                    "total_shares": 2_000,
                    "float_shares": 1_600,
                },
                {
                    "symbol": "000003.SZ",
                    "name": "基础快照保留股",
                    "close": 30.0,
                    "change_pct": -0.01,
                    "amount": 60_000,
                    "volume": 2_000,
                    "total_shares": 3_000,
                    "float_shares": 2_400,
                },
            ])

    monkeypatch.setattr(screener_api, "ScreenerService", _ScreenerService)

    quote_tick_store.append_many(
        tmp_path,
        [
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 10.0,
                "prev_close": 9.5,
                "amount": 100_000,
                "volume": 10_000,
                "timestamp": _ms(9, 30),
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 10.5,
                "prev_close": 9.5,
                "amount": 180_000,
                "volume": 18_000,
                "timestamp": _ms(9, 31),
            },
            {
                "symbol": "000002.SZ",
                "name": "万科A",
                "last_price": 20.0,
                "prev_close": 19.0,
                "amount": 90_000,
                "volume": 4_500,
                "timestamp": _ms(9, 30, 30),
            },
        ],
        source="tdxapi",
        force_flush=True,
    )

    payload = screener_api.market_snapshot(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        as_of_ts=_ms(9, 30, 45),
    )

    rows = {row["symbol"]: row for row in payload["rows"]}
    assert payload["mode"] == "intraday"
    assert payload["as_of"] == TRADE_DATE.isoformat()
    assert payload["as_of_ts"] == _ms(9, 30, 30)
    assert payload["count"] == 3
    assert rows["000001.SZ"]["close"] == 10.0
    assert rows["000002.SZ"]["close"] == 20.0
    assert rows["000003.SZ"]["close"] == 30.0
    assert rows["000003.SZ"]["amount"] == 60_000


def test_market_intraday_timeline_returns_historical_points(tmp_path):
    quote_tick_store.append_many(
        tmp_path,
        [
            {"symbol": "000001.SZ", "last_price": 10.0, "timestamp": _ms(9, 30)},
            {"symbol": "000001.SZ", "last_price": 10.2, "timestamp": _ms(9, 31)},
        ],
        source="tdxapi",
        force_flush=True,
    )

    payload = screener_api.market_intraday_timeline(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert payload["has_ticks"] is True
    assert payload["points"] == [_ms(9, 30), _ms(9, 31)]


def test_market_intraday_timeline_reads_after_today_realtime_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(screener_api, "_cn_today_safe", lambda: TRADE_DATE)
    monkeypatch.setattr(screener_api, "_is_realtime_collection_target", lambda target: target == TRADE_DATE)
    calls = []

    class _QuoteService:
        def _fetch_quotes(self):
            calls.append(True)
            quote_tick_store.append_many(
                tmp_path,
                [
                    {"symbol": "000001.SZ", "last_price": 10.0, "timestamp": _ms(9, 30)},
                    {"symbol": "000002.SZ", "last_price": 20.0, "timestamp": _ms(9, 31)},
                ],
                source="tdxapi",
                force_flush=True,
            )

    payload = screener_api.market_intraday_timeline(
        _request(tmp_path, quote_service=_QuoteService()),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert calls == [True]
    assert payload["has_ticks"] is True
    assert payload["points"] == [_ms(9, 30), _ms(9, 31)]


def test_market_intraday_timeline_fetches_today_when_ticks_are_sparse(monkeypatch, tmp_path):
    from app.services import quote_tick_backfill

    monkeypatch.setattr(screener_api, "_cn_today_safe", lambda: TRADE_DATE)
    monkeypatch.setattr(screener_api, "_is_realtime_collection_target", lambda target: target == TRADE_DATE)
    captured = {}
    calls = []

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    symbols = [f"000{i:03d}.SZ" for i in range(40)]
    pl.DataFrame({"symbol": symbols}).write_parquet(inst_dir / "instruments.parquet")
    quote_tick_store.append_many(
        tmp_path,
        [{"symbol": "000001.SZ", "last_price": 10.0, "timestamp": _ms(9, 30)}],
        source="tdxapi",
        force_flush=True,
    )

    class _QuoteService:
        def _fetch_quotes(self):
            calls.append(True)
            quote_tick_store.append_many(
                tmp_path,
                [
                    {"symbol": symbol, "last_price": 10.0, "timestamp": _ms(9, 31)}
                    for symbol in symbols
                ],
                source="tdxapi",
                force_flush=True,
            )

    def reject_enqueue(*args, **kwargs):
        captured["enqueue"] = {"args": args, "kwargs": kwargs}
        raise AssertionError("实时全市场拉取补足后不应再排队补数")

    monkeypatch.setattr(quote_tick_backfill.quote_tick_backfill_service, "enqueue", reject_enqueue)

    payload = screener_api.market_intraday_timeline(
        _request(tmp_path, quote_service=_QuoteService()),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert calls == [True]
    assert captured == {}
    assert payload["has_ticks"] is True
    assert payload["backfill_status"] == "ready"
    assert payload["symbol_count"] == 40


def test_market_intraday_timeline_queues_historical_backfill_when_missing(monkeypatch, tmp_path):
    from app.services import quote_tick_backfill

    captured = {}
    monkeypatch.setattr(screener_api, "_cn_today_safe", lambda: date(2026, 7, 31))

    def fake_enqueue(data_dir, target_date, **kwargs):
        captured["data_dir"] = data_dir
        captured["target_date"] = target_date
        captured["kwargs"] = kwargs
        return {"status": "queued", "date": target_date.isoformat(), "reason": kwargs.get("reason")}

    monkeypatch.setattr(quote_tick_backfill.quote_tick_backfill_service, "enqueue", fake_enqueue)

    payload = screener_api.market_intraday_timeline(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert payload["has_ticks"] is False
    assert payload["backfill_status"] == "queued"
    assert payload["backfill"]["reason"] == "timeline_missing_quote_ticks"
    assert captured["data_dir"] == tmp_path
    assert captured["target_date"] == TRADE_DATE


def test_market_intraday_timeline_requeues_sparse_minute_backfill(monkeypatch, tmp_path):
    from app.services import quote_tick_backfill

    captured = {}
    monkeypatch.setattr(screener_api, "_cn_today_safe", lambda: date(2026, 7, 31))

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({"symbol": [f"000{i:03d}.SZ" for i in range(40)]}).write_parquet(inst_dir / "instruments.parquet")
    quote_tick_store.append_many(
        tmp_path,
        [{"symbol": "000001.SZ", "last_price": 10.0, "timestamp": _ms(9, 30)}],
        source=quote_tick_store.MINUTE_BACKFILL_SOURCE,
        force_flush=True,
    )

    def fake_enqueue(data_dir, target_date, **kwargs):
        captured["data_dir"] = data_dir
        captured["target_date"] = target_date
        captured["kwargs"] = kwargs
        return {
            "status": "queued",
            "date": target_date.isoformat(),
            "reason": kwargs.get("reason"),
            "min_symbols": kwargs.get("min_symbols"),
        }

    monkeypatch.setattr(quote_tick_backfill.quote_tick_backfill_service, "enqueue", fake_enqueue)

    payload = screener_api.market_intraday_timeline(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert payload["has_ticks"] is True
    assert payload["backfill_status"] == "partial_ticks"
    assert payload["backfill"]["reason"] == "timeline_sparse_quote_ticks"
    assert payload["backfill"]["min_symbols"] == 20
    assert captured["data_dir"] == tmp_path
    assert captured["target_date"] == TRADE_DATE
    assert captured["kwargs"]["force"] is True
    assert captured["kwargs"]["min_symbols"] == 20


def test_market_intraday_timeline_queues_today_backfill_after_close(monkeypatch, tmp_path):
    from app.services import quote_tick_backfill

    captured = {}
    monkeypatch.setattr(screener_api, "_cn_today_safe", lambda: TRADE_DATE)
    monkeypatch.setattr(screener_api, "_is_realtime_collection_target", lambda target: False)

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({"symbol": [f"000{i:03d}.SZ" for i in range(40)]}).write_parquet(inst_dir / "instruments.parquet")
    quote_tick_store.append_many(
        tmp_path,
        [{"symbol": "000001.SZ", "last_price": 10.0, "timestamp": _ms(9, 30)}],
        source="tdxapi",
        force_flush=True,
    )

    def fake_enqueue(data_dir, target_date, **kwargs):
        captured["target_date"] = target_date
        captured["kwargs"] = kwargs
        return {"status": "queued", "date": target_date.isoformat(), "reason": kwargs.get("reason")}

    monkeypatch.setattr(quote_tick_backfill.quote_tick_backfill_service, "enqueue", fake_enqueue)

    payload = screener_api.market_intraday_timeline(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert payload["has_ticks"] is True
    assert payload["backfill_status"] == "partial_ticks"
    assert payload["backfill"]["reason"] == "timeline_sparse_quote_ticks"
    assert captured["target_date"] == TRADE_DATE


def test_market_intraday_timeline_backfills_from_local_minute(monkeypatch, tmp_path):
    class _ScreenerService:
        def __init__(self, _repo):
            pass

        def latest_date(self):
            return TRADE_DATE

        def _load_enriched_for_date(self, _target):
            return pl.DataFrame([
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "close": 9.8,
                    "change_pct": -0.02,
                    "amount": 80_000,
                    "volume": 8_000,
                },
                {
                    "symbol": "000002.SZ",
                    "name": "万科A",
                    "close": 19.5,
                    "change_pct": -0.025,
                    "amount": 70_000,
                    "volume": 3_500,
                },
            ])

    monkeypatch.setattr(screener_api, "ScreenerService", _ScreenerService)
    minute_dir = tmp_path / "kline_minute" / f"date={TRADE_DATE.isoformat()}"
    minute_dir.mkdir(parents=True)
    pl.DataFrame([
        {"symbol": "000001.SZ", "datetime": datetime(2026, 7, 8, 9, 30), "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 100.0, "amount": 1_000.0},
        {"symbol": "000001.SZ", "datetime": datetime(2026, 7, 8, 9, 31), "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 120.0, "amount": 1_224.0},
        {"symbol": "000002.SZ", "datetime": datetime(2026, 7, 8, 9, 30), "open": 20.0, "high": 20.1, "low": 19.9, "close": 20.0, "volume": 200.0, "amount": 4_000.0},
    ]).write_parquet(minute_dir / "part.parquet")

    daily_dir = tmp_path / "kline_daily" / "date=2026-07-07"
    daily_dir.mkdir(parents=True)
    pl.DataFrame([
        {"symbol": "000001.SZ", "close": 10.0},
        {"symbol": "000002.SZ", "close": 19.0},
    ]).write_parquet(daily_dir / "part.parquet")

    timeline = screener_api.market_intraday_timeline(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        step_seconds=60,
    )

    assert timeline["has_ticks"] is True
    assert timeline["backfill_status"] == "materialized"
    assert timeline["symbol_count"] == 2
    assert quote_tick_store.MINUTE_BACKFILL_SOURCE in timeline["sources"]
    assert timeline["points"] == [_ms(9, 30), _ms(9, 31)]

    snapshot = screener_api.market_snapshot(
        _request(tmp_path),
        as_of=TRADE_DATE.isoformat(),
        as_of_ts=_ms(9, 31),
    )
    rows = {row["symbol"]: row for row in snapshot["rows"]}
    assert snapshot["mode"] == "intraday"
    assert rows["000001.SZ"]["close"] == 10.2
    assert rows["000001.SZ"]["amount"] == 2_224.0
    assert rows["000001.SZ"]["change_pct"] == pytest.approx(0.02)
    assert rows["000002.SZ"]["close"] == 20.0
