"""分钟红7 — 开盘 N 根 (当日最早) 分钟K多数收红, 且最高的 top_red 根全红。

数据契约: filter_minute_history 接收当日全市场分钟K窗口
(symbol, datetime, open, high, low, close, volume, amount),
由 ScreenerService.build_strategy_context 的 1m 分支从本地 kline_minute
分区注入; 策略本身不感知数据来源 (本地同步 / 盘中增量刷新对它透明)。
"""

import polars as pl

META = {
    "id": "minute_red_streak",
    "name": "分钟红7",
    "description": "开盘前7根1分钟K至少5根收红, 且最高的2根(按最高价)都是红K",
    "tags": ["分钟", "形态", "短线"],
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    "params": [
        {
            "id": "bars",
            "label": "开盘K线数",
            "type": "int",
            "default": 7,
            "min": 5,
            "max": 15,
            "step": 1,
        },
        {
            "id": "min_red",
            "label": "最少红K数",
            "type": "int",
            "default": 5,
            "min": 1,
            "max": 15,
            "step": 1,
        },
        {
            "id": "top_red",
            "label": "最高K需红数",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 3,
            "step": 1,
        },
        {
            "id": "rank_by_close",
            "label": "最高K按收盘价排序",
            "type": "bool",
            "default": False,
        },
    ],
    "order_by": "red_count",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS: list[str] = []
EXIT_SIGNALS: list[str] = []


def filter_minute_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """红K形态过滤: 全向量化, 无逐行 Python 循环。

    - 每标的按时间取当日最早 bars 根 (开盘窗口); 不足 bars 根不触发
    - 红 = close > open; 窗口内红K数 >= min_red
    - 按 rank_by (high / close) 降序取前 top_red 根, 同值取时间更晚者, 需全红
    """
    bars = int(params.get("bars") or 7)
    min_red = min(int(params.get("min_red") or 5), bars)
    top_red = min(int(params.get("top_red") or 2), bars)
    rank_by = "close" if params.get("rank_by_close") else "high"
    if rank_by not in df.columns:
        rank_by = "high"

    windowed = (
        df.sort(["symbol", "datetime"])
        .filter(pl.int_range(pl.len()).over("symbol") < bars)
        .with_columns(_red=(pl.col("close") > pl.col("open")).cast(pl.Int32))
    )

    window = windowed.group_by("symbol").agg(
        bars_checked=pl.len(),
        red_count=pl.col("_red").sum(),
        last_datetime=pl.col("datetime").max(),
        # 输出列名用 close: 基础过滤的股价区间作用于开盘窗口末根收盘价
        close=pl.col("close").sort_by("datetime").last(),
        window_high=pl.col("high").max(),
        window_low=pl.col("low").min(),
        window_volume=pl.col("volume").sum(),
        window_amount=pl.col("amount").sum(),
    )

    top = (
        windowed.sort([rank_by, "datetime"], descending=[True, True])
        .filter(pl.int_range(pl.len()).over("symbol") < top_red)
        .group_by("symbol")
        .agg(top_red_count=pl.col("_red").sum())
    )

    return (
        window.join(top, on="symbol", how="inner")
        .filter(
            (pl.col("bars_checked") >= bars)
            & (pl.col("red_count") >= min_red)
            & (pl.col("top_red_count") >= top_red)
        )
        .drop("bars_checked")
    )
