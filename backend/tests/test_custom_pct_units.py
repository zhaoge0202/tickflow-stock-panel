"""自定义源实时行情涨跌幅单位自适应归一测试。

契约要求 change_pct/amplitude/turnover_rate 用小数制 (0.0366 = 3.66%),
但不少第三方接口(如 a-stock-data)直接返回 3.66 表示 3.66%。未归一会把
行业/概念统计与前端 x100 展示整体放大 100 倍(用户反馈)。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.provider import GenericHTTPProvider, _normalize_pct_units


def _df(pcts, amps=None, turnovers=None):
    data = {"change_pct": pcts}
    if amps is not None:
        data["amplitude"] = amps
    if turnovers is not None:
        data["turnover_rate"] = turnovers
    return pl.DataFrame(data)


def test_percent_unit_batch_is_divided_by_100():
    out = _normalize_pct_units(_df(
        [1.5, -2.2, 0.9, 2.8, -1.1, 0.6, 3.3, -0.8],
        amps=[2.0, 3.5, 1.8, 4.0, 2.5, 1.2, 5.0, 1.6],
        turnovers=[0.5, 1.2, 0.8, 2.0, 0.9, 0.4, 1.5, 0.7],
    ))
    assert out["change_pct"][0] == pytest.approx(0.015)
    assert out["amplitude"][0] == pytest.approx(0.02)
    assert out["turnover_rate"][0] == pytest.approx(0.005)


def test_fraction_unit_batch_untouched():
    pcts = [0.015, -0.022, 0.009, 0.028, -0.011, 0.006, 0.033, -0.008]
    out = _normalize_pct_units(_df(pcts, amps=[0.02, 0.035, 0.018, 0.04, 0.025, 0.012, 0.05, 0.016]))
    assert out["change_pct"].to_list() == pcts
    assert out["amplitude"][0] == pytest.approx(0.02)


def test_limit_up_fraction_30cm_not_divided():
    # 北交所 30% 涨跌停的小数制极值不应被误判为百分制
    out = _normalize_pct_units(_df([0.30, 0.29, 0.28, 0.27, 0.26]))
    assert out["change_pct"].to_list() == [0.30, 0.29, 0.28, 0.27, 0.26]


def test_small_batch_uses_max():
    # <5 样本退用最大值: 百分制小盘整批归一
    out = _normalize_pct_units(_df([0.5, 0.2]))
    assert out["change_pct"].to_list() == [pytest.approx(0.005), pytest.approx(0.002)]
    # 小数制小样本不动
    out2 = _normalize_pct_units(_df([0.005, 0.002]))
    assert out2["change_pct"].to_list() == [0.005, 0.002]


def test_string_values_are_cast():
    out = _normalize_pct_units(_df(["1.5", "-2.2", "0.9", "2.8", "3.3", "0.6"]))
    assert out["change_pct"][0] == pytest.approx(0.015)


def test_missing_or_null_columns_noop():
    out = _normalize_pct_units(pl.DataFrame({"close": [1.0, 2.0]}))
    assert out.columns == ["close"]
    out2 = _normalize_pct_units(_df([None, None, None, None, None, None]))
    assert out2["change_pct"].null_count() == 6


def _realtime_provider(rows):
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="pct_source",
        display_name="Pct Source",
        datasets={"realtime": DatasetConfig(
            url="https://example.test/realtime",
            field_map={
                "code": "symbol", "price": "last_price", "pre_close": "prev_close",
                "pct": "change_pct", "amp": "amplitude", "turnover": "turnover_rate",
            },
        )},
    ))
    provider._request_rows = lambda cfg, **kwargs: rows
    return provider


def test_get_realtime_normalizes_percent_source():
    provider = _realtime_provider([
        {"code": "S1", "price": 10.0, "pre_close": 9.85, "pct": 1.52, "amp": 2.4, "turnover": 1.1},
        {"code": "S2", "price": 20.0, "pre_close": 20.44, "pct": -2.15, "amp": 3.1, "turnover": 0.8},
        {"code": "S3", "price": 30.0, "pre_close": 29.8, "pct": 0.67, "amp": 1.9, "turnover": 0.5},
        {"code": "S4", "price": 40.0, "pre_close": 38.9, "pct": 2.83, "amp": 4.2, "turnover": 2.0},
        {"code": "S5", "price": 50.0, "pre_close": 50.55, "pct": -1.09, "amp": 2.0, "turnover": 0.9},
        {"code": "S6", "price": 60.0, "pre_close": 59.64, "pct": 0.60, "amp": 1.6, "turnover": 0.7},
    ])
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["S1"]["change_pct"] == pytest.approx(0.0152)
    assert by_sym["S1"]["amplitude"] == pytest.approx(0.024)
    assert by_sym["S1"]["turnover_rate"] == pytest.approx(0.011)
    assert by_sym["S2"]["change_pct"] == pytest.approx(-0.0215)
