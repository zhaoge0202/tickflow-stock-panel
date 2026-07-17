from datetime import date

import polars as pl

from app.services import rps_rotation


class _Repo:
    def enriched_latest_date(self):
        return date(2026, 7, 8)

    def get_enriched_range(self, start, end, symbols=None, columns=None):
        assert columns == ["symbol", "date", "close"]
        return pl.DataFrame({
            "symbol": ["A.SZ", "A.SZ", "A.SZ", "B.SZ", "B.SZ", "B.SZ"],
            "date": [
                date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8),
                date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8),
            ],
            "close": [10.0, 11.0, 12.1, 20.0, 18.0, 19.8],
        })


def test_rps_rotation_computes_change_pct_from_narrow_close_scan(monkeypatch):
    rps_rotation.invalidate_cache()
    map_df = pl.DataFrame({
        "_sym_up": ["A.SZ", "B.SZ"],
        "concept": ["上涨概念", "波动概念"],
    })
    monkeypatch.setattr(rps_rotation, "_load_concept_map_df", lambda repo: (map_df, 2))

    result = rps_rotation.build_rps_rotation(_Repo(), days=7)

    assert result["dates"][:2] == ["2026-07-08", "2026-07-07"]
    latest = dict(result["columns"]["2026-07-08"])
    assert round(latest["上涨概念"], 6) == 0.1
    assert round(latest["波动概念"], 6) == 0.1
