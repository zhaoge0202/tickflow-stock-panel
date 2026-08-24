"""市场情绪周期阶段(market_phase)单元测试 — 梯队指标与阶段规则引擎。"""
from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import polars as pl

from app.services.market_phase import (
    CLIMAX_GE2,
    EBB_PROMO,
    ICE_FIRST_BOARD,
    ICE_GE2,
    ICE_HEIGHT,
    PHASE_CLIMAX,
    PHASE_EBB,
    PHASE_ICE,
    PHASE_IGNITE,
    PHASE_RALLY,
    PHASE_REPAIR,
    classify_phase_series,
    finalize_ladder_row,
    with_prev_consecutive,
)
from app.services.regime_builder import _aggregate_daily, refresh_phase_labels, regime_path


def _days(n: int, start: str = "2024-01-01") -> list[date]:
    d0 = date.fromisoformat(start)
    out, cur = [], d0
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _frame(rows: list[dict]) -> pl.DataFrame:
    base = {
        "change_pct": 0.01, "amount": 1e8, "close": 10.0, "ma20": 9.5,
        "signal_limit_up": True, "signal_limit_down": False,
        "signal_broken_limit_up": False,
    }
    return pl.DataFrame([{**base, **r} for r in rows])


class TestLadderMetrics:
    def test_prev_consecutive_and_counts(self):
        d1, d2, d3 = _days(3)
        df = _frame([
            {"symbol": "A", "date": d1, "consecutive_limit_ups": 1},
            {"symbol": "A", "date": d2, "consecutive_limit_ups": 2},
            {"symbol": "A", "date": d3, "consecutive_limit_ups": 0},
            {"symbol": "B", "date": d1, "consecutive_limit_ups": 1},
            {"symbol": "B", "date": d2, "consecutive_limit_ups": 0},
            {"symbol": "C", "date": d2, "consecutive_limit_ups": 3},
        ])
        out = with_prev_consecutive(df)
        prev = {r["symbol"] + str(r["date"]): r["_prev_consec"] for r in out.iter_rows(named=True)}
        assert prev["A" + str(d1)] is None
        assert prev["A" + str(d2)] == 1
        assert prev["B" + str(d2)] == 1

        agg = _aggregate_daily(df)
        by_date = {r["date"]: r for r in agg.iter_rows(named=True)}
        assert by_date[d1]["first_board"] == 2
        assert by_date[d1]["ge2_count"] == 0
        # d2: A=2板(晋级), B=断板(昨1今0), C=3板(新面孔); pool=2(A,B) <10 → promo null
        assert by_date[d2]["ge2_count"] == 2
        assert by_date[d2]["promo_pool"] == 2
        assert by_date[d2]["promo_rate"] is None
        assert by_date[d2]["ladder_completeness"] == 1.0  # 档位 {2,3}, height=3 → 2/2
        assert by_date[d3]["first_board"] == 0

    def test_promo_rate_with_sufficient_pool(self):
        d1, d2 = _days(2)
        rows = []
        for i in range(12):
            rows.append({"symbol": f"S{i}", "date": d1, "consecutive_limit_ups": 1})
            rows.append({
                "symbol": f"S{i}", "date": d2,
                "consecutive_limit_ups": 2 if i < 4 else 0,  # 4 晋级, 8 断板
            })
        agg = _aggregate_daily(_frame(rows))
        d2_row = {r["date"]: r for r in agg.iter_rows(named=True)}[d2]
        assert d2_row["promo_pool"] == 12
        assert d2_row["promo_rate"] == round(4 / 12, 4)

    def test_promo_small_pool_is_null(self):
        row = {"promo_pool": 5, "promo_ok": 5, "max_consecutive": 3, "rungs_filled": 2}
        assert finalize_ladder_row(row)["promo_rate"] is None
        row2 = {"promo_pool": 20, "promo_ok": 8, "max_consecutive": 3, "rungs_filled": 2}
        assert finalize_ladder_row(row2)["promo_rate"] == 0.4

    def test_completeness_gap(self):
        # height=5, 只有 2板和5板 → rungs {2,5} → 2/4
        row = {"max_consecutive": 5, "rungs_filled": 2, "promo_pool": 0, "promo_ok": 0}
        assert finalize_ladder_row(row)["ladder_completeness"] == 0.5


def _series(specs: list[dict]) -> pl.DataFrame:
    """specs: [{days, height, first, ge2, promo, seal, state}] 逐段展开成日序。"""
    rows = []
    for sp in specs:
        for _ in range(sp["days"]):
            rows.append({
                "date": None,  # 由调用方生成后填充
                "max_consecutive": sp["height"],
                "first_board": sp["first"],
                "ge2_count": sp["ge2"],
                "promo_rate": sp["promo"],
                "seal_rate": sp["seal"],
                **({"state": sp.get("state", "range")} if sp.get("state") else {}),
            })
    dates = _days(len(rows))
    for r, d in zip(rows, dates, strict=True):
        r["date"] = d
    return pl.DataFrame(rows)


class TestClassifyPhaseSeries:
    def _labels(self, specs):
        df = _series(specs)
        return classify_phase_series(df)["phase"].to_list()

    def test_climax_and_persistence(self):
        labels = self._labels([
            {"days": 6, "height": 5, "first": 40, "ge2": 12, "promo": 0.2, "seal": 0.6, "state": "strong"},
            {"days": 5, "height": 12, "first": 300, "ge2": CLIMAX_GE2 + 40, "promo": 0.5, "seal": 0.7, "state": "strong"},
            {"days": 8, "height": 5, "first": 40, "ge2": 12, "promo": 0.2, "seal": 0.6, "state": "range"},
        ])
        assert PHASE_CLIMAX in labels
        assert labels[-1] == PHASE_REPAIR
        # EMA 平滑 + 2 日确认: 阶段切换总数有限, 不出现 1 日翻转噪声
        switches = sum(1 for a, b in pairwise(labels) if a != b)
        assert switches <= 4

    def test_rally_positive_state(self):
        labels = self._labels([
            {"days": 8, "height": 8, "first": 60, "ge2": 18, "promo": 0.3, "seal": 0.7, "state": "strong"},
        ])
        assert PHASE_RALLY in labels

    def test_rally_vetoed_in_weak_state(self):
        labels = self._labels([
            {"days": 8, "height": 8, "first": 60, "ge2": 18, "promo": 0.3, "seal": 0.7, "state": "weak"},
        ])
        assert PHASE_RALLY not in labels
        assert all(p == PHASE_REPAIR for p in labels)

    def test_ice(self):
        labels = self._labels([
            {"days": 6, "height": ICE_HEIGHT - 1, "first": ICE_FIRST_BOARD - 1,
             "ge2": ICE_GE2 - 1, "promo": 0.1, "seal": 0.5, "state": "range"},
        ])
        assert PHASE_ICE in labels

    def test_ebb_from_high(self):
        labels = self._labels([
            {"days": 8, "height": 9, "first": 60, "ge2": 20, "promo": 0.3, "seal": 0.7, "state": "strong"},
            {"days": 6, "height": 6, "first": 30, "ge2": 6, "promo": EBB_PROMO - 0.03, "seal": 0.5, "state": "range"},
        ])
        assert PHASE_EBB in labels

    def test_ignite_expansion(self):
        labels = self._labels([
            {"days": 6, "height": 4, "first": 25, "ge2": 5, "promo": 0.15, "seal": 0.6, "state": "range"},
            {"days": 8, "height": 6, "first": 50, "ge2": 14, "promo": 0.24, "seal": 0.68, "state": "lean_strong"},
        ])
        assert PHASE_IGNITE in labels

    def test_promo_null_leading_days_ffilled(self):
        specs = [
            {"days": 4, "height": 5, "first": 40, "ge2": 10, "promo": None, "seal": 0.6, "state": "range"},
            {"days": 4, "height": 6, "first": 50, "ge2": 14, "promo": 0.25, "seal": 0.68, "state": "strong"},
        ]
        df = _series(specs)
        out = classify_phase_series(df)
        assert out["phase"].null_count() == 0


class TestRefreshPhaseLabels:
    def test_roundtrip_writes_phase_keeps_state(self, tmp_path):
        specs = [
            {"days": 10, "height": 8, "first": 60, "ge2": 18, "promo": 0.3, "seal": 0.7, "state": "strong"},
        ]
        df = _series(specs)
        regime_path(tmp_path).parent.mkdir(parents=True)
        df.write_parquet(regime_path(tmp_path))
        n = refresh_phase_labels(tmp_path)
        assert n == 10
        out = pl.read_parquet(regime_path(tmp_path))
        assert "phase" in out.columns
        assert PHASE_RALLY in set(out["phase"].to_list())
        assert set(out["state"].to_list()) == {"strong"}

    def test_missing_columns_returns_zero(self, tmp_path):
        regime_path(tmp_path).parent.mkdir(parents=True)
        pl.DataFrame({"date": _days(3), "state": ["range"] * 3}).write_parquet(regime_path(tmp_path))
        assert refresh_phase_labels(tmp_path) == 0
