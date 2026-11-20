"""用分笔成交重建分钟 K。

通达信源头的分钟 K 偶发整段缺失 (例: 2026-10-23 全市场只回 13:41 之后的 80 根,
加 `limit` 也拿不到上午), 但同一天的 `/api/minute-trade-all` 分笔成交是完整的。
这里把分笔按分钟桶聚合成 1 分钟 K 并合并进 kline_minute 分区。

口径 (拿 2026-10-21 / 10-22 两天与源头分钟 K 逐根比对确认, 239/239 根成交量与
收盘完全一致):

  - 分笔时间戳是分钟精度, 落在整分上; 挂在 T+1 分钟桶 —— 即 `truncate + 1min`;
  - 集合竞价与 09:30 前的成交并入首根 09:31;
  - 11:31 归 11:30、15:01 归 15:00 (收盘集合竞价按笔回填的标签);
  - 15:00 之后的盘后固定价格成交 (Status 5) 全部丢弃, 与源头分钟 K 口径一致;
  - 价格 /1000 转元、成交量按手、成交额按元 (provider._trade_tick_row 已换算)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.parquet import atomic_update_parquet

logger = logging.getLogger(__name__)

_SCHEMA = {
    "symbol": pl.Utf8,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def _minute_partition(data_dir: Path, trade_date: date) -> Path:
    return Path(data_dir) / "kline_minute" / f"date={trade_date.isoformat()}" / "part.parquet"


def bars_from_ticks(ticks: list[dict], *, symbol: str, trade_date: date) -> pl.DataFrame:
    """把分笔成交聚合成 1 分钟 K (口径见模块 docstring)。"""
    rows = [
        {
            "dt": tick["datetime"],
            "price": float(tick["price"] or 0.0),
            "volume": float(tick["volume"] or 0.0),
            "amount": float(tick["amount"] or 0.0),
        }
        for tick in ticks
        if tick.get("datetime") is not None and tick.get("price")
    ]
    if not rows:
        return pl.DataFrame(schema=_SCHEMA)

    day = datetime(trade_date.year, trade_date.month, trade_date.day)
    session_open = day.replace(hour=9, minute=31)
    session_close = day.replace(hour=15, minute=0)
    remap = {
        day.replace(hour=11, minute=31): day.replace(hour=11, minute=30),
        day.replace(hour=15, minute=1): session_close,
    }

    frame = pl.DataFrame(
        rows,
        schema={"dt": pl.Datetime("us"), "price": pl.Float64, "volume": pl.Float64, "amount": pl.Float64},
    )
    frame = (
        frame.with_columns((pl.col("dt").dt.truncate("1m") + pl.duration(minutes=1)).alias("minute"))
        .with_columns(pl.col("minute").replace_strict(remap, default=pl.col("minute")).alias("minute"))
        .with_columns(
            pl.when(pl.col("minute") < session_open)
            .then(pl.lit(session_open))
            .otherwise(pl.col("minute"))
            .alias("minute")
        )
        .filter(pl.col("minute") <= session_close)
        .sort("dt")
    )
    return (
        frame.group_by("minute")
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("volume").sum().alias("volume"),
            pl.col("amount").sum().alias("amount"),
        )
        .rename({"minute": "datetime"})
        .with_columns(pl.lit(symbol).alias("symbol"))
        .sort("datetime")
        .select(list(_SCHEMA))
        .cast(_SCHEMA)
    )


def rebuild_minute_from_ticks(
    data_dir: Path,
    symbol: str,
    trade_date: date,
    *,
    overwrite: bool = False,
    provider=None,
) -> dict:
    """拉某票某日的分笔成交, 聚合成分钟 K 并合并进当日分区。

    overwrite=False (默认) 只补缺失的分钟, 已有的 (symbol, datetime) 一律保留。
    overwrite=True 用分笔派生值整体替换该票当日的分钟 K —— 当库里那几根本来
    来自别的口径、与分笔对不上时用这个 (例如 2026-10-23 上午那批)。
    """
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        raise ValueError("symbol 不能为空")

    if provider is None:
        from app.plugins.tdxapi.provider import TDXAPIProvider

        provider = TDXAPIProvider()
    ticks = provider.get_trade_ticks(symbol, trade_date, mode="all", limit=None)
    incoming = bars_from_ticks(ticks, symbol=symbol, trade_date=trade_date)
    result = {
        "symbol": symbol,
        "date": trade_date.isoformat(),
        "ticks": len(ticks or []),
        "bars": int(incoming.height),
        "overwrite": overwrite,
    }
    if incoming.is_empty():
        return {**result, "status": "no_ticks", "existing": 0, "written": 0}

    out = _minute_partition(data_dir, trade_date)

    def _merge(existing: pl.DataFrame) -> pl.DataFrame:
        if existing.is_empty():
            return incoming.sort(["symbol", "datetime"])
        result["existing"] = int(existing.filter(pl.col("symbol") == symbol).height)
        others = existing.filter(pl.col("symbol") != symbol)
        if overwrite:
            merged = incoming
        else:
            same = existing.filter(pl.col("symbol") == symbol)
            missing = incoming.join(
                same.select(["symbol", "datetime"]), on=["symbol", "datetime"], how="anti",
            )
            merged = pl.concat([same, missing], how="diagonal_relaxed")
        return pl.concat([others, merged], how="diagonal_relaxed").sort(["symbol", "datetime"])

    result.setdefault("existing", 0)
    _, after = atomic_update_parquet(out, _merge)
    final = pl.read_parquet(out, columns=["symbol", "datetime"])
    written = int(final.filter(pl.col("symbol") == symbol).height)
    logger.info(
        "rebuild_minute_from_ticks: %s %s 分笔 %d 笔 -> %d 根 (覆盖=%s), 分区内该票 %d -> %d 根",
        symbol, trade_date, result["ticks"], result["bars"], overwrite, result["existing"], written,
    )
    return {**result, "status": "ok", "written": written, "partition_rows": after}
