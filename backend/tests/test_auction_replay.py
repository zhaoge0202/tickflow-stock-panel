from __future__ import annotations

import sys
import types
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from app.api import replay as replay_api
from app.services import auction_replay, quote_tick_store, strategy_cache
from app.strategy.engine import StrategyResult

CN = ZoneInfo("Asia/Shanghai")
SIGNAL_DATE = date(2026, 7, 7)
TRADE_DATE = date(2026, 7, 8)


def _ms(hour: int, minute: int, second: int = 0, ms: int = 0) -> int:
    return int(datetime(2026, 7, 8, hour, minute, second, ms * 1000, tzinfo=CN).timestamp() * 1000)


def _cached() -> dict:
    return {
        "as_of": SIGNAL_DATE.isoformat(),
        "results": {
            "strategy_a": {
                "as_of": SIGNAL_DATE.isoformat(),
                "total": 2,
                "rows": [
                    {"symbol": "000001.SZ", "name": "平安银行", "score": 80.0},
                    {"symbol": "000002.SZ", "name": "万科A", "score": 70.0},
                ],
            },
        },
        "updated_at": 1,
    }


def _append_replay_ticks(tmp_path):
    quote_tick_store.append_many(
        tmp_path,
        [
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 9.8,
                "auction_price": 9.8,
                "auction_change_pct": -0.02,
                "auction_matched_volume": 800,
                "price_type": "auction_reference",
                "market_phase": "preopen_auction",
                "timestamp": _ms(9, 23, 10),
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 10.0,
                "auction_price": 10.0,
                "auction_change_pct": 0.0,
                "auction_matched_volume": 1000,
                "auction_unmatched_side": "buy",
                "auction_unmatched_volume": 300,
                "auction_pressure_score": 0.23,
                "price_type": "auction_reference",
                "market_phase": "preopen_auction",
                "timestamp": _ms(9, 24, 30),
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 12.0,
                "auction_price": 12.0,
                "auction_change_pct": 0.2,
                "auction_matched_volume": 9999,
                "price_type": "auction_reference",
                "market_phase": "preopen_auction",
                "timestamp": _ms(9, 25, 0),
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 10.2,
                "change_pct": 0.02,
                "volume": 1200,
                "amount": 122_400,
                "price_type": "trade",
                "timestamp": _ms(9, 25, 3),
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 10.4,
                "change_pct": 0.04,
                "volume": 1600,
                "amount": 166_400,
                "price_type": "trade",
                "timestamp": _ms(9, 27, 12),
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "last_price": 20.0,
                "change_pct": 1.0,
                "volume": 9999,
                "amount": 1_999_800,
                "price_type": "trade",
                "timestamp": _ms(9, 30, 0),
            },
            {
                "symbol": "000002.SZ",
                "name": "万科A",
                "last_price": 8.0,
                "auction_price": 8.0,
                "auction_change_pct": -0.01,
                "auction_matched_volume": 500,
                "price_type": "auction_reference",
                "market_phase": "preopen_auction",
                "timestamp": _ms(9, 24, 50),
            },
        ],
        source="tdxapi",
        force_flush=True,
    )


def _strategy_result(frame: dict) -> dict:
    return frame["results"]["strategy_a"]


def _history_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ"],
        "name": ["平安银行", "万科A"],
        "date": [SIGNAL_DATE, SIGNAL_DATE],
        "open": [10.0, 8.0],
        "high": [10.2, 8.1],
        "low": [9.9, 7.9],
        "close": [10.0, 8.0],
        "volume": [1000.0, 900.0],
        "amount": [1_000_000.0, 720_000.0],
        "raw_close": [10.0, 8.0],
        "raw_high": [10.2, 8.1],
        "raw_low": [9.9, 7.9],
        "auction_result_price": [99.0, 88.0],
        "auction_result_volume": [1.0, 1.0],
        "auction_result_amount": [99.0, 88.0],
    })


class _DynamicEngine:
    def __init__(self) -> None:
        self.run_count = 0
        self.update_count = 0

    def has(self, _strategy_id: str) -> bool:
        return True

    def required_history_bars(self, *_args, **_kwargs) -> int:
        return 2

    def prepare_realtime_matrix(self, *_args, **_kwargs):
        self.update_count += 1
        return None

    def realtime_matrix_stats(self, _cache_key: str) -> dict[str, int]:
        return {"generation": self.update_count, "build_count": 1, "update_count": self.update_count}

    def run_all(self, context, *_args, strategy_ids=None, **_kwargs):
        self.run_count += 1
        rows = []
        for row in context.current.to_dicts():
            if row["symbol"] == "000001.SZ" and row.get("snapshot_price_type") == "trade":
                rows.append({**row, "score": 88.0})
        sid = (strategy_ids or ["custom_dual_edge"])[0]
        return {
            sid: StrategyResult(
                as_of=TRADE_DATE,
                strategy_id=sid,
                rows=rows,
                total=len(rows),
                elapsed_ms=1.0,
            )
        }


def test_dynamic_history_reuses_same_request_window(tmp_path):
    auction_replay._dynamic_history_cache.clear()
    calls = 0

    class _Svc:
        asset_type = "stock"
        repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

        def _load_enriched_history(self, target_date: date, lookback_days: int) -> pl.DataFrame:
            nonlocal calls
            calls += 1
            assert target_date == SIGNAL_DATE
            assert lookback_days == 2
            return _history_frame()

        def _load_enriched_for_date(self, _target_date: date) -> pl.DataFrame:
            raise AssertionError("should load history window")

    engine = _DynamicEngine()
    first = auction_replay._load_dynamic_history(
        _Svc(),
        engine,
        SIGNAL_DATE,
        ["custom_dual_edge"],
        params_map={"custom_dual_edge": {}},
        overrides_map={"custom_dual_edge": {}},
    )
    second = auction_replay._load_dynamic_history(
        _Svc(),
        engine,
        SIGNAL_DATE,
        ["custom_dual_edge"],
        params_map={"custom_dual_edge": {}},
        overrides_map={"custom_dual_edge": {}},
    )

    assert calls == 1
    assert first is second


def test_dynamic_quote_window_reuses_parquet_cache(monkeypatch, tmp_path):
    auction_replay._quote_window_cache.clear()
    calls = 0
    parquet_rows = [
        {
            "symbol": "000001.SZ",
            "event_ts": _ms(9, 25, 1),
            "ingest_ts": 1,
            "source": "tdxapi",
            "price_type": "trade",
            "last_price": 10.0,
        },
        {
            "symbol": "000002.SZ",
            "event_ts": _ms(9, 29, 59),
            "ingest_ts": 1,
            "source": "tdxapi",
            "price_type": "trade",
            "last_price": 8.0,
        },
    ]

    def fake_scan(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [dict(row) for row in parquet_rows]

    monkeypatch.setattr(
        auction_replay,
        "_quote_window_fingerprint",
        lambda _base: (("part.parquet", 128, 1),),
    )
    monkeypatch.setattr(auction_replay, "_scan_quote_window_rows", fake_scan)
    monkeypatch.setattr(quote_tick_store, "_hot_rows", lambda *_args, **_kwargs: [])

    first = auction_replay._read_quote_window_rows(
        tmp_path,
        TRADE_DATE,
        start_ts=_ms(9, 25, 0),
        end_ts=_ms(9, 25, 3),
    )
    second = auction_replay._read_quote_window_rows(
        tmp_path,
        TRADE_DATE,
        start_ts=_ms(9, 25, 0),
        end_ts=_ms(9, 30, 0),
    )

    assert calls == 1
    assert [row["symbol"] for row in first] == ["000001.SZ"]
    assert [row["symbol"] for row in second] == ["000001.SZ", "000002.SZ"]


def test_auction_replay_uses_dense_seconds_without_faking_events(tmp_path):
    _append_replay_ticks(tmp_path)

    payload = auction_replay.replay_cached_strategy_results(
        tmp_path,
        _cached(),
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
        include_frames=False,
    )

    assert payload["status"] == "ready"
    assert payload["frame_mode"] == "dense_seconds"
    assert payload["timeline_sparse"] is False
    assert payload["missing_seconds_are_carried_forward"] is True
    assert len(payload["timeline"]) == 420

    gap_point = next(point for point in payload["timeline"] if point["time"] == "09:25:04")
    assert gap_point["has_event"] is False
    assert gap_point["auction_event_count"] == 0
    assert gap_point["trade_event_count"] == 0

    final_row = _strategy_result(payload["final_frame"])["rows"][0]
    assert final_row["symbol"] == "000001.SZ"
    assert final_row["auction_price"] == 10.0
    assert final_row["auction_event_time"] == "09:24:30"
    assert final_row["open_confirm_price"] == 10.4
    assert final_row["open_confirm_time"] == "09:27:12"
    assert final_row["open_confirm_stale_seconds"] > 0


def test_auction_replay_updates_as_soon_as_trade_snapshot_arrives(tmp_path):
    _append_replay_ticks(tmp_path)

    frame_0924 = auction_replay.replay_cached_strategy_results(
        tmp_path,
        _cached(),
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
        as_of_ts=_ms(9, 24, 0),
        include_candidates=True,
    )["frame"]
    row_0924 = _strategy_result(frame_0924)["candidates"][0]
    assert row_0924["auction_event_time"] == "09:23:10"
    assert row_0924["auction_stale_seconds"] == 50
    assert row_0924["auction_replay_status"] == "pending_trade"

    frame_092440 = auction_replay.replay_cached_strategy_results(
        tmp_path,
        _cached(),
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
        as_of_ts=_ms(9, 24, 40),
        include_candidates=True,
    )["frame"]
    row_092440 = _strategy_result(frame_092440)["candidates"][0]
    assert row_092440["auction_price"] == 10.0
    assert row_092440["auction_event_time"] == "09:24:30"

    frame_092502 = auction_replay.replay_cached_strategy_results(
        tmp_path,
        _cached(),
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
        as_of_ts=_ms(9, 25, 2),
        include_candidates=True,
    )["frame"]
    result_092502 = _strategy_result(frame_092502)
    assert result_092502["confirmed_total"] == 0
    assert result_092502["pending_trade_total"] == 2
    assert result_092502["candidates"][0]["auction_price"] == 10.0

    frame_092503 = auction_replay.replay_cached_strategy_results(
        tmp_path,
        _cached(),
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["strategy_a"],
        as_of_ts=_ms(9, 25, 3),
    )["frame"]
    result_092503 = _strategy_result(frame_092503)
    assert result_092503["confirmed_total"] == 1
    assert result_092503["pending_trade_total"] == 1
    assert result_092503["rows"][0]["symbol"] == "000001.SZ"
    assert result_092503["rows"][0]["open_confirm_price"] == 10.2
    assert result_092503["rows"][0]["open_confirm_time"] == "09:25:03"


def test_auction_replay_api_reads_strategy_cache(tmp_path):
    _append_replay_ticks(tmp_path)
    strategy_cache.write_cache(tmp_path, SIGNAL_DATE.isoformat(), _cached()["results"])
    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )

    payload = replay_api.run_auction(
        replay_api.AuctionReplayReq(
            as_of=SIGNAL_DATE,
            trade_date=TRADE_DATE,
            strategy_ids=["strategy_a"],
            as_of_ts=_ms(9, 25, 3),
        ),
        req,
    )

    assert payload["status"] == "ready"
    assert payload["frame"]["results"]["strategy_a"]["confirmed_total"] == 1


def test_auction_dynamic_recomputes_after_trade_snapshot(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    _append_replay_ticks(tmp_path)
    monkeypatch.setattr(
        auction_replay,
        "_load_dynamic_history",
        lambda *_args, **_kwargs: _history_frame(),
    )
    engine = _DynamicEngine()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    before_trade = auction_replay.replay_dynamic_strategy_results(
        repo,
        engine,
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["custom_dual_edge"],
        as_of_ts=_ms(9, 25, 2),
        include_candidates=True,
    )
    before_result = before_trade["frame"]["results"]["custom_dual_edge"]
    assert before_trade["status"] == "awaiting_trade"
    assert before_result["candidate_total"] == 0
    assert before_result["final_total"] == 0

    after_trade = auction_replay.replay_dynamic_strategy_results(
        repo,
        engine,
        as_of=SIGNAL_DATE,
        trade_date=TRADE_DATE,
        strategy_ids=["custom_dual_edge"],
        as_of_ts=_ms(9, 25, 3),
        include_candidates=True,
    )
    after_result = after_trade["frame"]["results"]["custom_dual_edge"]

    assert after_trade["status"] == "ready"
    assert engine.run_count == 2
    assert after_result["candidate_total"] == 1
    assert after_result["final_total"] == 1
    assert after_result["dual_rows"][0]["symbol"] == "000001.SZ"
    assert after_result["rows"][0]["symbol"] == "000001.SZ"
    assert after_result["rows"][0]["auction_result_price"] == 10.0
    assert after_result["rows"][0]["auction_result_volume"] == 1000
    assert after_result["rows"][0]["auction_event_time"] == "09:24:30"
    assert after_result["rows"][0]["open_confirm_time"] == "09:25:03"
    assert after_result["rows"][0]["amount"] > after_result["rows"][0]["open_confirm_amount"]
