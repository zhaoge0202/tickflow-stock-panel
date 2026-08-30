"""自定义源实时行情比例字段单位归一测试 (CONTRIBUTING §3.1)。

契约: change_pct/amplitude/turnover_rate 为小数制 (0.0366 = 3.66%)。
单位只认显式声明 pct_unit: percent|decimal, 不靠数值猜:
  - 声明 percent → 无条件 /100; 声明 decimal → 无条件透传;
  - 未声明 → change_pct 保留截面中位数判定(涨跌停 30% 上限物理可判),
    amplitude/turnover_rate 置 None 交下游重算(fail-closed),
    已被 transforms 显式处理过的列视为用户接管单位, 透传。
"""

from __future__ import annotations

import polars as pl
import pytest

from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig, config_from_dict
from app.data_providers.custom.provider import GenericHTTPProvider, _normalize_pct_units


def _df(pcts, amps=None, turnovers=None):
    data = {"change_pct": pcts}
    if amps is not None:
        data["amplitude"] = amps
    if turnovers is not None:
        data["turnover_rate"] = turnovers
    return pl.DataFrame(data)


# ---- 显式声明: percent ----


def test_declared_percent_divides_all_columns():
    out = _normalize_pct_units(
        _df(
            [1.5, -2.2, 0.9, 2.8, -1.1, 0.6, 3.3, -0.8],
            amps=[2.0, 3.5, 1.8, 4.0, 2.5, 1.2, 5.0, 1.6],
            turnovers=[0.5, 1.2, 0.8, 2.0, 0.9, 0.4, 1.5, 0.7],
        ),
        pct_unit="percent",
    )
    assert out["change_pct"][0] == pytest.approx(0.015)
    assert out["amplitude"][0] == pytest.approx(0.02)
    assert out["turnover_rate"][0] == pytest.approx(0.005)


def test_declared_percent_wins_even_when_values_look_decimal():
    # 百分制低波动日: 0.25 表示 0.25%, 数值落在小数制区间内——声明优先, 不靠猜
    out = _normalize_pct_units(
        _df(
            [0.25, 0.30, 0.28, 0.27, 0.26, 0.22],
            amps=[0.4, 0.5, 0.45, 0.6, 0.5, 0.4],
            turnovers=[0.05, 0.08, 0.06, 0.1, 0.07, 0.05],
        ),
        pct_unit="percent",
    )
    assert out["change_pct"][0] == pytest.approx(0.0025)
    assert out["amplitude"][0] == pytest.approx(0.004)
    assert out["turnover_rate"][0] == pytest.approx(0.0005)


# ---- 显式声明: decimal ----


def test_declared_decimal_passes_through():
    pcts = [0.015, -0.022, 0.009, 0.028, -0.011, 0.006, 0.033, -0.008]
    out = _normalize_pct_units(
        _df(pcts, amps=[0.02, 0.035, 0.018, 0.04, 0.025, 0.012, 0.05, 0.016]), pct_unit="decimal"
    )
    assert out["change_pct"].to_list() == pcts
    assert out["amplitude"][0] == pytest.approx(0.02)


def test_declared_decimal_wins_even_when_values_look_percent():
    # 用户声明了小数制就按小数制契约透传, 不替用户"修正"数据
    out = _normalize_pct_units(_df([3.66, -2.15, 0.9, 2.8, 1.1]), pct_unit="decimal")
    assert out["change_pct"][0] == pytest.approx(3.66)


# ---- 未声明: change_pct 保留截面判定(物理可判) ----


def test_undeclared_change_pct_percent_batch_normalized():
    out = _normalize_pct_units(_df([1.5, -2.2, 0.9, 2.8, 3.3, 0.6]))
    assert out["change_pct"][0] == pytest.approx(0.015)


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


# ---- 未声明: amplitude/turnover_rate fail-closed (核心修复) ----


def test_undeclared_amplitude_and_turnover_are_nulled():
    # 百分制 0.05 = 0.05% 与小数制 0.05 = 5% 数值相同, 不可判定 → 置 None
    out = _normalize_pct_units(
        _df(
            [1.5, -2.2, 0.9, 2.8, 3.3, 0.6],
            amps=[2.0, 3.5, 1.8, 4.0, 5.0, 1.6],
            turnovers=[0.05, 1.2, 0.8, 2.0, 1.5, 0.7],
        )
    )
    assert out["amplitude"].null_count() == 6
    assert out["turnover_rate"].null_count() == 6
    # change_pct 仍正常归一
    assert out["change_pct"][0] == pytest.approx(0.015)


def test_undeclared_transformed_column_passes_through():
    # 用户已用 transforms 显式处理过单位(如 value / 100)的列: 视为接管, 不置 None
    out = _normalize_pct_units(
        _df([1.5, -2.2, 0.9, 2.8, 3.3, 0.6], turnovers=[0.005, 0.012, 0.008, 0.02, 0.015, 0.007]),
        transformed_cols=frozenset({"turnover_rate"}),
    )
    assert out["turnover_rate"][0] == pytest.approx(0.005)
    # 未 transform 的 amplitude 仍 fail-closed
    assert "amplitude" not in out.columns


def test_missing_or_null_columns_noop():
    out = _normalize_pct_units(pl.DataFrame({"close": [1.0, 2.0]}))
    assert out.columns == ["close"]
    out2 = _normalize_pct_units(_df([None, None, None, None, None, None]))
    assert out2["change_pct"].null_count() == 6
    # 全 null 的不可判定列保持 null
    out3 = _normalize_pct_units(_df([1.5, -2.2, 0.9, 2.8, 3.3, 0.6], turnovers=[None] * 6))
    assert out3["turnover_rate"].null_count() == 6


# ---- provider 集成 ----


def _realtime_provider(rows, **ds_kwargs):
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="pct_source",
            display_name="Pct Source",
            datasets={
                "realtime": DatasetConfig(
                    url="https://example.test/realtime",
                    field_map={
                        "code": "symbol",
                        "price": "last_price",
                        "pre_close": "prev_close",
                        "pct": "change_pct",
                        "amp": "amplitude",
                        "turnover": "turnover_rate",
                    },
                    **ds_kwargs,
                )
            },
        )
    )
    provider._request_rows = lambda cfg, **kwargs: rows
    return provider


_ROWS = [
    {"code": "S1", "price": 10.0, "pre_close": 9.85, "pct": 1.52, "amp": 2.4, "turnover": 1.1},
    {"code": "S2", "price": 20.0, "pre_close": 20.44, "pct": -2.15, "amp": 3.1, "turnover": 0.8},
    {"code": "S3", "price": 30.0, "pre_close": 29.8, "pct": 0.67, "amp": 1.9, "turnover": 0.5},
    {"code": "S4", "price": 40.0, "pre_close": 38.9, "pct": 2.83, "amp": 4.2, "turnover": 2.0},
    {"code": "S5", "price": 50.0, "pre_close": 50.55, "pct": -1.09, "amp": 2.0, "turnover": 0.9},
    {"code": "S6", "price": 60.0, "pre_close": 59.64, "pct": 0.60, "amp": 1.6, "turnover": 0.7},
]


def test_get_realtime_declared_percent_source():
    provider = _realtime_provider(_ROWS, pct_unit="percent")
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["S1"]["change_pct"] == pytest.approx(0.0152)
    assert by_sym["S1"]["amplitude"] == pytest.approx(0.024)
    assert by_sym["S1"]["turnover_rate"] == pytest.approx(0.011)
    assert by_sym["S2"]["change_pct"] == pytest.approx(-0.0215)


def test_get_realtime_undeclared_nulls_ambiguous_columns():
    provider = _realtime_provider(_ROWS)
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    # change_pct 截面判定仍归一
    assert by_sym["S1"]["change_pct"] == pytest.approx(0.0152)
    # 不可判定列 fail-closed
    assert by_sym["S1"]["amplitude"] is None
    assert by_sym["S1"]["turnover_rate"] is None


def test_get_realtime_transformed_turnover_kept():
    provider = _realtime_provider(_ROWS, transforms={"turnover_rate": "value / 100"})
    try:
        rows = provider.get_realtime()
    finally:
        provider.close()
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["S1"]["turnover_rate"] == pytest.approx(0.011)
    assert by_sym["S1"]["amplitude"] is None


# ---- 配置解析与校验 ----


def test_config_parses_pct_unit():
    cfg = config_from_dict(
        {
            "name": "s",
            "datasets": {
                "realtime": {
                    "url": "https://example.test",
                    "pct_unit": "Percent",
                }
            },
        }
    )
    assert cfg.datasets["realtime"].pct_unit == "percent"


def test_config_rejects_invalid_pct_unit():
    with pytest.raises(ValueError, match="pct_unit"):
        config_from_dict(
            {
                "name": "s",
                "datasets": {
                    "realtime": {
                        "url": "https://example.test",
                        "pct_unit": "basis_point",
                    }
                },
            }
        )


def test_validate_flags_pct_unit_on_non_realtime():
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="s",
            display_name="S",
            datasets={
                "daily": DatasetConfig(
                    url="https://example.test",
                    field_map={
                        "c": "symbol",
                        "d": "date",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "cl": "close",
                        "v": "volume",
                        "a": "amount",
                    },
                    pct_unit="percent",
                )
            },
        )
    )
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("pct_unit" in e and "realtime" in e for e in errors)


def test_validate_flags_invalid_pct_unit_value():
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="s",
            display_name="S",
            datasets={
                "realtime": DatasetConfig(
                    url="https://example.test",
                    field_map={
                        "c": "symbol",
                        "p": "last_price",
                        "pc": "prev_close",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "v": "volume",
                    },
                    pct_unit="bp",
                )
            },
        )
    )
    try:
        errors = provider.validate()
    finally:
        provider.close()
    assert any("pct_unit" in e for e in errors)
