from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import screener as screener_api
from app.services import auction_preselect


def _strategy():
    return SimpleNamespace(
        basic_filter={
            "price_min": 5.0,
            "price_max": 80.0,
            "amount_min": 1.5e8,
            "exclude_st": True,
            "boards": ["沪主板", "深主板", "创业板", "科创板"],
        },
        meta={"params": []},
    )


def test_preselect_rows_force_main_board_and_exclude_st():
    frame = pl.DataFrame([
        {
            "symbol": "688001.SH",
            "name": "科创强票",
            "open": 20.0,
            "high": 22.0,
            "low": 19.8,
            "close": 21.8,
            "amount": 8e8,
            "change_pct": 0.08,
            "ma20": 19.0,
            "vol_ratio_5d": 2.2,
            "annual_vol_20d": 0.5,
            "momentum_20d": 0.08,
        },
        {
            "symbol": "300001.SZ",
            "name": "创业强票",
            "open": 12.0,
            "high": 13.0,
            "low": 11.9,
            "close": 12.8,
            "amount": 6e8,
            "change_pct": 0.06,
            "ma20": 11.8,
            "vol_ratio_5d": 2.0,
            "annual_vol_20d": 0.4,
            "momentum_20d": 0.05,
        },
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "open": 10.0,
            "high": 10.8,
            "low": 9.9,
            "close": 10.7,
            "amount": 4e8,
            "change_pct": 0.055,
            "ma20": 10.1,
            "vol_ratio_5d": 1.8,
            "annual_vol_20d": 0.45,
            "momentum_20d": 0.04,
        },
        {
            "symbol": "600000.SH",
            "name": "浦发银行",
            "open": 8.0,
            "high": 8.4,
            "low": 7.9,
            "close": 8.3,
            "amount": 3e8,
            "change_pct": 0.035,
            "ma20": 8.0,
            "vol_ratio_5d": 1.2,
            "annual_vol_20d": 0.35,
            "momentum_20d": 0.02,
        },
        {
            "symbol": "001234.SZ",
            "name": "*ST测试",
            "open": 7.0,
            "high": 7.6,
            "low": 6.9,
            "close": 7.5,
            "amount": 5e8,
            "change_pct": 0.07,
            "ma20": 7.0,
            "vol_ratio_5d": 2.0,
            "annual_vol_20d": 0.3,
            "momentum_20d": 0.1,
        },
    ])

    rows = auction_preselect._preselect_rows(
        frame,
        strategy_id="custom_dual_edge",
        strategy=_strategy(),
        params={},
        overrides={},
        limit=5,
    )

    assert [row["symbol"] for row in rows] == ["000001.SZ", "600000.SH"]
    assert all(row["preselect_status"] == "watch_only" for row in rows)


def test_preselect_rows_avoid_abnormal_follows_strategy_params():
    frame = pl.DataFrame([
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "open": 10.0,
            "high": 10.8,
            "low": 9.9,
            "close": 10.7,
            "amount": 4e8,
            "change_pct": 0.055,
            "ma20": 10.1,
            "vol_ratio_5d": 1.8,
            "annual_vol_20d": 0.45,
            "momentum_20d": 0.04,
            "_pre_cum9": 0.15,
        },
        {
            "symbol": "600000.SH",
            "name": "临近严重异动票",
            "open": 8.0,
            "high": 8.4,
            "low": 7.9,
            "close": 8.3,
            "amount": 3e8,
            "change_pct": 0.035,
            "ma20": 8.0,
            "vol_ratio_5d": 1.2,
            "annual_vol_20d": 0.35,
            "momentum_20d": 0.02,
            "_pre_cum9": 0.95,
        },
    ])

    # 策略开了 avoid_abnormal: 9日累计 95% 的票被剔出预选池
    rows = auction_preselect._preselect_rows(
        frame,
        strategy_id="custom_dual_edge_focus",
        strategy=_strategy(),
        params={"avoid_abnormal": True, "abnormal_cum9_max": 60.0},
        overrides={},
        limit=5,
    )
    assert [row["symbol"] for row in rows] == ["000001.SZ"]

    # 策略没有该参数(如 dual_edge 主策略): 行为不变
    rows_off = auction_preselect._preselect_rows(
        frame,
        strategy_id="custom_dual_edge",
        strategy=_strategy(),
        params={},
        overrides={},
        limit=5,
    )
    assert [row["symbol"] for row in rows_off] == ["000001.SZ", "600000.SH"]


def test_preselect_payload_uses_next_trade_day_and_main_board_only(monkeypatch, tmp_path):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
    )

    class _Engine:
        def has(self, _sid):
            return True

        def get(self, _sid):
            return _strategy()

        def validate_context(self, *_args):
            return None

        def resolve_params(self, *_args, **_kwargs):
            return {}

    svc = SimpleNamespace(
        latest_date=lambda: date(2026, 8, 11),
        build_strategy_context=lambda *_args, **_kwargs: SimpleNamespace(
            current=pl.DataFrame([
                {
                    "symbol": "688001.SH",
                    "name": "科创强票",
                    "open": 20.0,
                    "high": 22.0,
                    "low": 19.8,
                    "close": 21.8,
                    "amount": 8e8,
                    "change_pct": 0.08,
                    "ma20": 19.0,
                    "vol_ratio_5d": 2.2,
                    "annual_vol_20d": 0.5,
                    "momentum_20d": 0.08,
                    "date": date(2026, 8, 11),
                },
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "open": 10.0,
                    "high": 10.8,
                    "low": 9.9,
                    "close": 10.7,
                    "amount": 4e8,
                    "change_pct": 0.055,
                    "ma20": 10.1,
                    "vol_ratio_5d": 1.8,
                    "annual_vol_20d": 0.45,
                    "momentum_20d": 0.04,
                    "date": date(2026, 8, 11),
                },
            ]),
            history=pl.DataFrame(),
        ),
    )

    monkeypatch.setattr(auction_preselect.strategy_config, "list_overrides", lambda *_args: {})
    monkeypatch.setattr(auction_preselect, "ScreenerService", lambda *_args, **_kwargs: svc)

    payload = auction_preselect.build_preselect_payload(
        repo,
        _Engine(),
        as_of=date(2026, 8, 11),
        trade_date=date(2026, 8, 12),
        strategy_ids=["custom_dual_edge"],
        limit_per_strategy=5,
        asset_type="stock",
    )

    result = payload["results"]["custom_dual_edge"]
    assert payload["trade_date"] == "2026-08-12"
    assert [row["symbol"] for row in result["rows"]] == ["000001.SZ"]


def test_preselect_payload_ignores_strategies_without_auction_algorithm(monkeypatch, tmp_path):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
    )

    class _Engine:
        def has(self, _sid):
            return True

    monkeypatch.setattr(auction_preselect.strategy_config, "list_overrides", lambda *_args: {})
    monkeypatch.setattr(auction_preselect, "ScreenerService", lambda *_args, **_kwargs: SimpleNamespace(
        latest_date=lambda: date(2026, 8, 11),
    ))

    payload = auction_preselect.build_preselect_payload(
        repo,
        _Engine(),
        as_of=date(2026, 8, 11),
        trade_date=date(2026, 8, 12),
        strategy_ids=["low_volatility_leader"],
    )

    assert payload["status"] == "empty_strategies"
    assert payload["results"] == {}


class _MonitorEngine:
    def latest_strategy_results(self):
        return {}


def _request(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, monitor_engine=_MonitorEngine(), strategy_engine=object())
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_preselect_api_applies_ext_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(
        screener_api,
        "build_preselect_payload",
        lambda *_args, **_kwargs: {
            "mode": "preselect",
            "as_of": "2026-08-11",
            "trade_date": "2026-08-12",
            "updated_at": 1,
            "results": {
                "strategy_a": {
                    "strategy": "strategy_a",
                    "as_of": "2026-08-11",
                    "trade_date": "2026-08-12",
                    "total": 1,
                    "preselect_total": 1,
                    "rows": [{"symbol": "000001.SZ"}],
                },
            },
        },
    )
    monkeypatch.setattr(
        screener_api,
        "_load_ext_value_maps",
        lambda *_args, **_kwargs: {"concept.name": {"000001.SZ": "银行"}},
    )

    payload = screener_api.preselect(
        screener_api.PreselectRequest(
            as_of=None,
            trade_date=None,
            strategy_ids=["strategy_a"],
            ext_columns="concept.name",
            asset_type="stock",
        ),
        _request(tmp_path),
    )

    assert payload["mode"] == "preselect"
    assert payload["results"]["strategy_a"]["rows"] == [{"symbol": "000001.SZ", "concept.name": "银行"}]
