"""因子注册表 (L-REG) P1 收口快照测试。

黄金数据为收口前 factor.py / scoring.py 的字面量副本。
任何目录漂移 (id/label/group/desc/顺序/依赖/预热) 都必须在改动前更新这里的黄金数据,
保证历史候选方案引用的因子 id 与挖掘调度顺序 (FACTOR_COLUMNS[:48]) 不受影响。
"""
from __future__ import annotations

import pytest

from app.factors.registry import (
    FactorSpec,
    all_factors,
    factor_columns_view,
    factor_dependencies,
    get_factor,
    register_factor,
    scoring_warmups,
    virtual_dependencies,
)

# --- 黄金数据: 收口前 factor.py FACTOR_COLUMNS 原文 ---
GOLDEN_COLUMNS: list[dict] = [
    {"id": "momentum_5d", "label": "5日动量", "group": "动量", "desc": "5个交易日累计收益率"},
    {"id": "momentum_10d", "label": "10日动量", "group": "动量", "desc": "10个交易日累计收益率"},
    {"id": "momentum_20d", "label": "20日动量", "group": "动量", "desc": "20个交易日累计收益率"},
    {"id": "momentum_30d", "label": "30日动量", "group": "动量", "desc": "30个交易日累计收益率"},
    {"id": "momentum_60d", "label": "60日动量", "group": "动量", "desc": "60个交易日累计收益率"},
    {"id": "change_pct", "label": "日涨跌幅", "group": "动量", "desc": "当日收盘相对前收盘的收益率"},
    {"id": "ma5_bias", "label": "MA5乖离", "group": "均线偏离", "desc": "收盘价 / MA5 - 1"},
    {"id": "ma10_bias", "label": "MA10乖离", "group": "均线偏离", "desc": "收盘价 / MA10 - 1"},
    {"id": "ma20_bias", "label": "MA20乖离", "group": "均线偏离", "desc": "收盘价 / MA20 - 1"},
    {"id": "ma30_bias", "label": "MA30乖离", "group": "均线偏离", "desc": "收盘价 / MA30 - 1"},
    {"id": "ma60_bias", "label": "MA60乖离", "group": "均线偏离", "desc": "收盘价 / MA60 - 1"},
    {"id": "ema5_bias", "label": "EMA5乖离", "group": "均线偏离", "desc": "收盘价 / EMA5 - 1"},
    {"id": "ema10_bias", "label": "EMA10乖离", "group": "均线偏离", "desc": "收盘价 / EMA10 - 1"},
    {"id": "ema20_bias", "label": "EMA20乖离", "group": "均线偏离", "desc": "收盘价 / EMA20 - 1"},
    {"id": "ema30_bias", "label": "EMA30乖离", "group": "均线偏离", "desc": "收盘价 / EMA30 - 1"},
    {"id": "ema60_bias", "label": "EMA60乖离", "group": "均线偏离", "desc": "收盘价 / EMA60 - 1"},
    {"id": "rsi_6", "label": "RSI(6)", "group": "超买超卖", "desc": "6日相对强弱指标"},
    {"id": "rsi_14", "label": "RSI(14)", "group": "超买超卖", "desc": "14日相对强弱指标"},
    {"id": "rsi_24", "label": "RSI(24)", "group": "超买超卖", "desc": "24日相对强弱指标"},
    {"id": "macd_hist", "label": "MACD柱(原值)", "group": "趋势", "desc": "兼容历史研究; 跨股票比较建议优先使用MACD柱强度"},
    {"id": "macd_dif_pct", "label": "MACD DIF强度", "group": "趋势", "desc": "MACD DIF / 收盘价"},
    {"id": "macd_dea_pct", "label": "MACD DEA强度", "group": "趋势", "desc": "MACD DEA / 收盘价"},
    {"id": "macd_hist_pct", "label": "MACD柱强度", "group": "趋势", "desc": "MACD柱 / 收盘价, 消除股价尺度影响"},
    {"id": "kdj_k", "label": "KDJ-K", "group": "趋势", "desc": "KDJ指标K值"},
    {"id": "kdj_d", "label": "KDJ-D", "group": "趋势", "desc": "KDJ指标D值"},
    {"id": "kdj_j", "label": "KDJ-J", "group": "趋势", "desc": "KDJ指标J值"},
    {"id": "boll_position", "label": "布林位置", "group": "趋势", "desc": "收盘价在布林带下轨到上轨之间的位置"},
    {"id": "annual_vol_20d", "label": "20日波动率", "group": "波动率", "desc": "20日收益率年化标准差"},
    {"id": "atr_14", "label": "ATR(14)原值", "group": "波动率", "desc": "兼容历史研究; 跨股票比较建议优先使用ATR相对波动"},
    {"id": "atr_pct", "label": "ATR相对波动", "group": "波动率", "desc": "ATR(14) / 收盘价"},
    {"id": "amplitude", "label": "日振幅", "group": "波动率", "desc": "当日高低价差 / 前收盘价"},
    {"id": "boll_width", "label": "布林带宽", "group": "波动率", "desc": "布林带上下轨宽度 / MA20"},
    {"id": "vol_ratio_5d", "label": "5日量比", "group": "量价", "desc": "当日成交量 / 前5日平均成交量"},
    {"id": "vol_ratio_10d", "label": "10日量比", "group": "量价", "desc": "当日成交量 / 前10日平均成交量"},
    {"id": "vol_trend_5_10", "label": "成交量趋势", "group": "量价", "desc": "5日平均成交量 / 10日平均成交量 - 1"},
    {"id": "turnover_rate", "label": "换手率", "group": "量价", "desc": "使用历史时点流通股本计算的当日换手率"},
    {"id": "turnover_ratio_5d", "label": "换手率放大", "group": "量价", "desc": "当日换手率 / 前5日平均换手率 - 1"},
    {"id": "log_amount", "label": "成交额对数", "group": "量价", "desc": "ln(成交额 + 1), 降低极端规模影响"},
    {"id": "amount_ratio_5d", "label": "成交额放大", "group": "量价", "desc": "当日成交额 / 前5日平均成交额 - 1"},
    {"id": "gap_return", "label": "开盘跳空", "group": "价格位置", "desc": "开盘价 / 前收盘价 - 1"},
    {"id": "intraday_return", "label": "日内收益", "group": "价格位置", "desc": "收盘价 / 开盘价 - 1"},
    {"id": "close_position", "label": "收盘位置", "group": "价格位置", "desc": "收盘价在当日最低价到最高价之间的位置"},
    {"id": "distance_to_high_60d", "label": "距60日高点", "group": "价格位置", "desc": "收盘价 / 60日最高收盘价 - 1"},
    {"id": "distance_from_low_60d", "label": "距60日低点", "group": "价格位置", "desc": "收盘价 / 60日最低收盘价 - 1"},
    {"id": "vwap_bias", "label": "VWAP乖离", "group": "价格位置", "desc": "收盘价 / 当日成交均价 - 1, 成交均价 = 成交额 / (成交量x100)"},
    {"id": "max_ret_20d", "label": "20日最大单日涨幅", "group": "收益形态", "desc": "近20个交易日单日涨幅最大值(彩票效应, 高值代表博彩型特征强)"},
    {"id": "ret_skew_20d", "label": "20日收益偏度", "group": "收益形态", "desc": "近20个交易日日收益偏度, 高值代表右偏(偶发大涨)"},
    {"id": "up_days_20d", "label": "20日上涨天数", "group": "收益形态", "desc": "近20个交易日中上涨天数(0~20)"},
    {"id": "amihud_20d", "label": "20日Amihud非流动性", "group": "流动性", "desc": "近20日平均 |日涨跌幅| / 成交额(亿元), 高值代表流动性差"},
    {"id": "turnover_z_60d", "label": "换手率60日z分", "group": "流动性", "desc": "(当日换手率 - 前60日均值) / 前60日标准差, 衡量换手异动"},
    {"id": "vol_price_corr_20d", "label": "20日量价相关", "group": "量价", "desc": "近20个交易日日涨跌幅与成交量的相关系数, 高值代表量价同向"},
    {"id": "vol_trend_5_60", "label": "量能趋势(5/60)", "group": "量价", "desc": "5日平均成交量 / 60日平均成交量 - 1"},
    {"id": "limit_up_count_20d", "label": "涨停基因(20日)", "group": "涨停基因", "desc": "近20个交易日涨停次数"},
    {"id": "limit_up_count_60d", "label": "涨停基因(60日)", "group": "涨停基因", "desc": "近60个交易日涨停次数"},
    {"id": "pb_latest", "label": "市净率(最新公告)", "group": "财务", "desc": "收盘价 / 最新已公告每股净资产; 无财务数据或公告前为空"},
    {"id": "roe_latest", "label": "ROE(最新公告)", "group": "财务", "desc": "最新已公告净资产收益率(%); 无财务数据或公告前为空"},
    {"id": "gross_margin_latest", "label": "毛利率(最新公告)", "group": "财务", "desc": "最新已公告销售毛利率(%)"},
    {"id": "net_margin_latest", "label": "净利率(最新公告)", "group": "财务", "desc": "最新已公告销售净利率(%)"},
    {"id": "revenue_yoy_latest", "label": "营收增速(最新公告)", "group": "财务", "desc": "最新已公告营业收入同比(%)"},
    {"id": "net_income_yoy_latest", "label": "净利增速(最新公告)", "group": "财务", "desc": "最新已公告归母净利润同比(%)"},
    {"id": "debt_ratio_latest", "label": "资产负债率(最新公告)", "group": "财务", "desc": "最新已公告资产负债率(%)"},
    # --- 扩充批次 (2026-09-05): 追加于目录尾部, 前 48 项挖掘调度顺序不变 ---
    {"id": "log_float_mv", "label": "流通市值对数", "group": "规模", "desc": "ln(收盘价 x 当日成交量 / 换手率), 由换手率反推流通股本, 高值代表大盘"},
    {"id": "momentum_120d", "label": "120日动量", "group": "动量", "desc": "120个交易日累计收益率 (中期动量, 与短窗口互补)"},
    {"id": "mom_accel_20_60", "label": "动量加速度", "group": "动量", "desc": "20日动量 - 60日动量, 衡量近期动量相对中期是否增强"},
    {"id": "rsi_14_delta_5d", "label": "RSI五日变化", "group": "超买超卖", "desc": "RSI(14) - 5日前的RSI(14), 衡量强弱指标的边际变化"},
    {"id": "overnight_ret_20d", "label": "20日隔夜收益", "group": "收益形态", "desc": "近20日累计隔夜收益(开盘价/前收盘-1求和), A股隔夜与日内收益的定价机制不同"},
    {"id": "intraday_ret_20d", "label": "20日日内收益", "group": "收益形态", "desc": "近20日累计日内收益(收盘价/开盘价-1求和), 与隔夜收益构成收益分解"},
    {"id": "downside_vol_20d", "label": "20日下行波动", "group": "波动率", "desc": "sqrt(近20日 min(日收益,0)^2 均值), 只度量下跌侧风险"},
    {"id": "vol_regime_5_60", "label": "波动率状态(5/60)", "group": "波动率", "desc": "5日收益标准差 / 60日收益标准差, 高值代表波动骤然放大"},
    {"id": "amplitude_trend_20_60", "label": "振幅趋势(20/60)", "group": "波动率", "desc": "20日平均振幅 / 60日平均振幅 - 1"},
    {"id": "obv_trend_20d", "label": "20日量能潮", "group": "量价", "desc": "近20日 sign(日收益)x成交量 之和 / (20日均量x20), 有界[-1,1], 净买入方向的一致性"},
    {"id": "amount_mean_20d", "label": "20日均成交额(亿)", "group": "量价", "desc": "近20日平均成交额(亿元), 规模/流动性水平量"},
    {"id": "turnover_mean_20d", "label": "20日均换手", "group": "流动性", "desc": "近20日平均换手率, A股经典低换手溢价因子"},
    {"id": "turnover_std_20d", "label": "20日换手波动", "group": "流动性", "desc": "近20日换手率标准差 / 均值 (变异系数), 衡量交易活跃的稳定性"},
    {"id": "position_240d", "label": "一年价格位置", "group": "价格位置", "desc": "收盘价在近240个交易日最低价到最高价之间的位置 (0~1)"},
    {"id": "distance_to_high_240d", "label": "距一年高点", "group": "价格位置", "desc": "收盘价 / 240日最高收盘价 - 1, 接近0代表贴近一年新高"},
    {"id": "kdj_kd_diff", "label": "KDJ K-D差", "group": "趋势", "desc": "KDJ K值 - D值, 正值代表快线在慢线上方"},
]

GOLDEN_VIRTUAL_DEPS: dict[str, frozenset[str]] = {
    **{
        f"ma{period}_bias": frozenset({"close", f"ma{period}"})
        for period in (5, 10, 20, 30, 60)
    },
    **{
        f"ema{period}_bias": frozenset({"close", f"ema{period}"})
        for period in (5, 10, 20, 30, 60)
    },
    "macd_dif_pct": frozenset({"close", "macd_dif"}),
    "macd_dea_pct": frozenset({"close", "macd_dea"}),
    "macd_hist_pct": frozenset({"close", "macd_hist"}),
    "boll_position": frozenset({"close", "boll_upper", "boll_lower"}),
    "atr_pct": frozenset({"close", "atr_14"}),
    "boll_width": frozenset({"ma20", "boll_upper", "boll_lower"}),
    "vol_ratio_10d": frozenset({"volume"}),
    "vol_trend_5_10": frozenset({"vol_ma5", "vol_ma10"}),
    "turnover_ratio_5d": frozenset({"turnover_rate"}),
    "log_amount": frozenset({"amount"}),
    "amount_ratio_5d": frozenset({"amount"}),
    "gap_return": frozenset({"open", "prev_close"}),
    "intraday_return": frozenset({"open", "close"}),
    "close_position": frozenset({"high", "low", "close"}),
    "distance_to_high_60d": frozenset({"close", "high_60d"}),
    "distance_from_low_60d": frozenset({"close", "low_60d"}),
    "max_ret_20d": frozenset({"close"}),
    "ret_skew_20d": frozenset({"close"}),
    "up_days_20d": frozenset({"close"}),
    "amihud_20d": frozenset({"close", "amount"}),
    "turnover_z_60d": frozenset({"turnover_rate"}),
    "vol_price_corr_20d": frozenset({"close", "volume"}),
    "vwap_bias": frozenset({"close", "volume", "amount"}),
    "vol_trend_5_60": frozenset({"volume"}),
    "limit_up_count_20d": frozenset({"consecutive_limit_ups"}),
    "limit_up_count_60d": frozenset({"consecutive_limit_ups"}),
    # --- 扩充批次 (2026-09-05) ---
    "log_float_mv": frozenset({"close", "volume", "turnover_rate"}),
    "momentum_120d": frozenset({"close"}),
    "mom_accel_20_60": frozenset({"momentum_20d", "momentum_60d"}),
    "rsi_14_delta_5d": frozenset({"rsi_14"}),
    "overnight_ret_20d": frozenset({"open", "prev_close"}),
    "intraday_ret_20d": frozenset({"open", "close"}),
    "downside_vol_20d": frozenset({"close"}),
    "vol_regime_5_60": frozenset({"close"}),
    "amplitude_trend_20_60": frozenset({"amplitude"}),
    "obv_trend_20d": frozenset({"close", "volume"}),
    "amount_mean_20d": frozenset({"amount"}),
    "turnover_mean_20d": frozenset({"turnover_rate"}),
    "turnover_std_20d": frozenset({"turnover_rate"}),
    "position_240d": frozenset({"close"}),
    "distance_to_high_240d": frozenset({"close"}),
    "kdj_kd_diff": frozenset({"kdj_k", "kdj_d"}),
}

GOLDEN_WARMUP: dict[str, int] = {
    "vol_ratio_10d": 11,
    "turnover_ratio_5d": 6,
    "amount_ratio_5d": 6,
    "max_ret_20d": 21,
    "ret_skew_20d": 21,
    "up_days_20d": 21,
    "amihud_20d": 21,
    "turnover_z_60d": 61,
    "vol_price_corr_20d": 21,
    "vol_trend_5_60": 60,
    "limit_up_count_20d": 21,
    "limit_up_count_60d": 61,
    # --- 扩充批次 (2026-09-05) ---
    "momentum_120d": 121,
    "rsi_14_delta_5d": 6,
    "overnight_ret_20d": 21,
    "intraday_ret_20d": 21,
    "downside_vol_20d": 21,
    "vol_regime_5_60": 61,
    "amplitude_trend_20_60": 61,
    "obv_trend_20d": 21,
    "amount_mean_20d": 21,
    "turnover_mean_20d": 21,
    "turnover_std_20d": 21,
    "position_240d": 241,
    "distance_to_high_240d": 241,
}


def test_factor_columns_snapshot() -> None:
    """注册表生成的 FACTOR_COLUMNS 与收口前字面量逐项一致 (含顺序)。"""
    from app.backtest.factor import FACTOR_COLUMNS

    assert FACTOR_COLUMNS == GOLDEN_COLUMNS
    assert factor_columns_view() == GOLDEN_COLUMNS


def test_virtual_dependencies_snapshot() -> None:
    """注册表生成的依赖声明与收口前字面量逐项一致。"""
    from app.strategy.scoring import VIRTUAL_SCORING_DEPENDENCIES

    assert VIRTUAL_SCORING_DEPENDENCIES == GOLDEN_VIRTUAL_DEPS
    assert virtual_dependencies() == GOLDEN_VIRTUAL_DEPS


def test_scoring_warmup_snapshot() -> None:
    from app.strategy.scoring import _ROLLING_SCORING_WARMUP

    assert _ROLLING_SCORING_WARMUP == GOLDEN_WARMUP
    assert scoring_warmups() == GOLDEN_WARMUP


def test_catalog_counts_and_kinds() -> None:
    specs = all_factors()
    assert len(specs) == 77
    assert len({spec.id for spec in specs}) == 77  # id 唯一
    virtual = [spec for spec in specs if spec.kind == "virtual"]
    assert len(virtual) == 52  # ma/ema 10 + 原有 26 + 扩充批次 16
    financial = [spec for spec in specs if spec.pit]
    assert len(financial) == 7
    assert all(spec.pit_source == "financial_announce" for spec in financial)
    assert all(spec.asset_types == frozenset({"stock"}) for spec in financial)


def test_mining_schedule_order_prefix_unchanged() -> None:
    """挖掘调度取 FACTOR_COLUMNS[:48], 首元素必须保持 momentum_5d。"""
    from app.backtest.factor import FACTOR_COLUMNS

    assert FACTOR_COLUMNS[0]["id"] == "momentum_5d"
    assert len(FACTOR_COLUMNS) >= 48


def test_get_factor_and_dependencies() -> None:
    spec = get_factor("ma20_bias")
    assert spec is not None
    assert spec.dependencies == frozenset({"close", "ma20"})
    assert spec.warmup_bars == 1  # 无滚动窗口, 与历史默认一致

    resolved = factor_dependencies(["ma20_bias", "rsi_14", "unknown_col"])
    assert resolved == frozenset({"close", "ma20", "rsi_14", "unknown_col"})


def test_asset_type_filter() -> None:
    stock = all_factors(asset_type="stock")
    etf = all_factors(asset_type="etf")
    assert len(stock) == 77
    assert len(etf) == 70  # 财务 7 项仅股票


def test_register_factor_rejects_duplicate() -> None:
    spec = get_factor("rsi_14")
    assert spec is not None
    with pytest.raises(ValueError, match="已注册"):
        register_factor(spec)


def test_register_factor_allows_version_bump() -> None:
    from app.factors import registry

    fresh = FactorSpec(id="__test_custom_factor", label="测试因子", group="测试", formula_text="close", kind="custom")
    register_factor(fresh)
    bumped = FactorSpec(
        id="__test_custom_factor", label="测试因子", group="测试", formula_text="close + 1",
        kind="custom", version=2,
    )
    register_factor(bumped)
    try:
        current = get_factor("__test_custom_factor")
        assert current is not None
        assert current.version == 2
        assert current.formula_text == "close + 1"
    finally:
        # 清理测试注册项; 目录视图 (_CATALOG) 不受 _REGISTRY 动态注册影响
        registry._REGISTRY.pop("__test_custom_factor", None)


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.factors import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_factors_api_contract() -> None:
    client = _client()
    response = client.get("/api/factors")
    assert response.status_code == 200
    payload = response.json()
    factors = payload["factors"]
    assert len(factors) == 77
    first = factors[0]
    assert first["id"] == "momentum_5d"
    assert first["kind"] == "base"
    assert first["formula"] == "5个交易日累计收益率"
    assert first["asset_types"] == ["etf", "stock"]
    ma20 = next(item for item in factors if item["id"] == "ma20_bias")
    assert ma20["kind"] == "virtual"
    assert ma20["dependencies"] == ["close", "ma20"]
    pb = next(item for item in factors if item["id"] == "pb_latest")
    assert pb["pit"] is True
    assert pb["asset_types"] == ["stock"]
    mv = next(item for item in factors if item["id"] == "log_float_mv")
    assert mv["kind"] == "virtual"
    assert mv["scale_free"] is False


def test_factors_api_asset_filter_and_validation() -> None:
    client = _client()
    etf = client.get("/api/factors", params={"asset_type": "etf"}).json()["factors"]
    assert len(etf) == 70
    assert all("stock" in item["asset_types"] for item in etf)
    # 非法资产类型 → 422 (fail-closed, 不静默回退全量)
    assert client.get("/api/factors", params={"asset_type": "index"}).status_code == 422
