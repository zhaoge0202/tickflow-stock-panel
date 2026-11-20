"""实时矩阵的量比必须按当日成交进度折算到全天量级。

盘中最后一行 volume 是累计部分量。不折算时量比被低估约 1/progress 倍,
`vol_ratio >= 1.55` 这类阈值要等到午后甚至尾盘才可能成立, 出票时点被系统性
推到日内高位 —— 与 indicators/pipeline.py 的 time_factor 口径必须一致。
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    RealtimeMarketDataMatrix,
    build_market_data_matrix,
    matrix_feature,
)


def _panel(volumes: list[float]) -> pl.DataFrame:
    rows = []
    for index, volume in enumerate(volumes):
        rows.append({
            "symbol": "A",
            "date": f"2026-11-{index + 1:02d}",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": volume,
        })
    return pl.DataFrame(rows)


def _market(volumes: list[float], *, progress: float | None = None):
    market = build_market_data_matrix(_panel(volumes), field_columns=set())
    if progress is None:
        return market
    return replace(market, live_bar_progress=progress)


# 前 5 日各 1000 手, 当日累计 500 手 (盘中约走完一半)
VOLUMES = [1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 500.0]


def test_intraday_progress_scales_only_the_live_bar():
    raw = matrix_feature(_market(VOLUMES), "vol_ratio_5d")
    # 未折算: 500 / 1000 = 0.5
    assert raw[-1] == pytest.approx(0.5)

    scaled = matrix_feature(_market(VOLUMES, progress=0.5), "vol_ratio_5d")
    # 折算后 = (500 / 0.5) / 1000 = 1.0, 与收盘口径可比
    assert scaled[-1] == pytest.approx(1.0)
    # 历史行本来就是完整日成交量, 不能被折算
    assert np.allclose(scaled[:-1], raw[:-1], equal_nan=True)


@pytest.mark.parametrize("progress", [None, 0.0, 1.0, 1.5])
def test_out_of_intraday_progress_leaves_volume_untouched(progress):
    raw = matrix_feature(_market(VOLUMES), "vol_ratio_5d")
    got = matrix_feature(_market(VOLUMES, progress=progress), "vol_ratio_5d")
    assert np.allclose(got, raw, equal_nan=True)


def test_realtime_snapshot_carries_live_progress():
    panel = _panel(VOLUMES)
    buffer = RealtimeMarketDataMatrix(panel, field_columns=set())

    plain = matrix_feature(buffer.snapshot(), "vol_ratio_5d")
    assert plain[-1] == pytest.approx(0.5)

    live = matrix_feature(buffer.snapshot(live_bar_progress=0.5), "vol_ratio_5d")
    assert live[-1] == pytest.approx(1.0)

    # 不传参数时沿用缓冲自身的进度 (无进度则不折算)
    assert buffer.snapshot().live_bar_progress is None


def test_vol_ratio_10d_scales_the_same_way():
    volumes = [1000.0] * 10 + [500.0]
    raw = matrix_feature(_market(volumes), "vol_ratio_10d")
    scaled = matrix_feature(_market(volumes, progress=0.5), "vol_ratio_10d")
    assert raw[-1] == pytest.approx(0.5)
    assert scaled[-1] == pytest.approx(1.0)


def test_engine_live_progress_from_quote_timestamp():
    """进度优先取行情真实时间; 收盘后/缺失时返回 None, 让量比按完整日线算。"""
    from datetime import datetime

    from app.market_time import CN_TZ
    from app.strategy.engine import _live_bar_progress

    # 10:00 → 已交易 30 分钟 → 30/240 = 0.125
    ten_am = int(datetime(2026, 11, 17, 10, 0, tzinfo=CN_TZ).timestamp() * 1000)
    assert _live_bar_progress(pl.DataFrame({"quote_ts": [ten_am]})) == pytest.approx(0.125)

    # 15:00 之后 → 最后一根已是完整日线
    after_close = int(datetime(2026, 11, 17, 15, 5, tzinfo=CN_TZ).timestamp() * 1000)
    assert _live_bar_progress(pl.DataFrame({"quote_ts": [after_close]})) is None
