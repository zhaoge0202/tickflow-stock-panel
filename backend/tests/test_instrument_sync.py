from app.services import instrument_sync as ins


def test_merge_instrument_rows_fills_missing_metadata_from_tickflow():
    provider_rows = [
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "code": "002491",
            "exchange": "SZ",
            "region": "CN",
            "type": "stock",
            "listing_date": None,
            "total_shares": None,
            "float_shares": None,
            "tick_size": None,
            "limit_up": None,
            "limit_down": None,
        },
    ]
    tickflow_rows = [
        {
            "symbol": "002491.SZ",
            "name": "通鼎互联",
            "code": "002491",
            "exchange": "SZ",
            "region": "CN",
            "type": "stock",
            "listing_date": "2011-05-18",
            "total_shares": 7638514276.0,
            "float_shares": 4386387432.0,
            "tick_size": 0.01,
            "limit_up": 26.54,
            "limit_down": 21.72,
        },
    ]

    merged = ins._merge_instrument_rows(provider_rows, tickflow_rows)

    assert len(merged) == 1
    row = merged[0]
    assert row["symbol"] == "002491.SZ"
    assert row["name"] == "通鼎互联"
    assert row["float_shares"] == 4386387432.0
    assert row["total_shares"] == 7638514276.0
    assert row["limit_up"] == 26.54
    assert row["limit_down"] == 21.72
