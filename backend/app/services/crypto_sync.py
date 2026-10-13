"""crypto 资产类别的行情同步 (永续合约)。

为什么独立于 services/kline_sync: 那条链路按 A 股口径写死 —— 交易日历门控、
09:25-15:05 分钟窗口、复权因子、涨跌停校验、按北京自然日分区。把 crypto 塞进去
需要给每个门控加资产分支, 而任意一处漏改都会产出静默错误结果 (CONTRIBUTING §3)。
本模块只做 crypto 需要的四件事, A 股链路保持零改动。

口径 (app/market_rules + data_providers/binance/provider.py):
  - 日K date = UTC 合约日;
  - 分钟 datetime = 北京墙钟 naive, 分区键 = 该分钟的 UTC 合约日;
  - 无复权因子同步 (永续无除权除息);
  - volume 保持上游口径 (币/张), 不按「手」换算。
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.data_providers import crypto_registry
from app.market_rules import CRYPTO, contract_day_start, to_contract_date
from app.parquet import atomic_write_parquet
from app.services import preferences

logger = logging.getLogger(__name__)

DEFAULT_DAILY_DAYS = 365
MINUTE_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def _provider():
    return crypto_registry.get_crypto_provider(preferences.get_crypto_data_provider())


def _minute_partition_dir(data_dir: Path, contract_day: date) -> Path:
    return Path(data_dir) / "kline_crypto_minute" / f"date={contract_day.isoformat()}"


def sync_crypto_instruments(repo) -> int:
    """拉取并保存永续合约维表。返回标的数。失败上抛 (维表缺失会让已有数据被误判资产类别)。"""
    rows = _provider().get_crypto_instruments()
    if not rows:
        logger.warning("crypto 维表为空, 跳过写入")
        return 0
    df = pl.DataFrame(rows).select(["symbol", "name", "code", "exchange", "asset_type", "source"])
    repo.save_crypto_instruments(df)
    logger.info("crypto 维表已同步: %d 个合约", df.height)
    return df.height


def sync_crypto_daily(
    repo,
    symbols: list[str] | None = None,
    days: int = DEFAULT_DAILY_DAYS,
) -> int:
    """同步日K (UTC 合约日)。返回写入行数。

    symbols 缺省取本地 crypto 维表; 维表为空时返回 0, 不猜标的池。
    """
    target = [str(s) for s in (symbols or sorted(repo.get_crypto_symbol_set())) if s]
    if not target:
        logger.warning("crypto 日K同步跳过: 标的池为空 (需先同步维表)")
        return 0
    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=max(1, int(days)))
    df = _provider().get_daily(target, start_date=start_date, end_date=end_date)
    if df.is_empty():
        logger.warning("crypto 日K同步: 窗口 %s~%s 无数据", start_date, end_date)
        return 0
    # 防御: date 列必须是合约日口径 (provider 契约), 越界行直接丢弃而不是改写
    df = df.filter(pl.col("date") >= start_date)
    repo.append_crypto_daily(df)
    logger.info("crypto 日K已同步: %d 行 / %d 标的", df.height, len(target))
    return df.height


def write_crypto_minute_partitions(repo, df: pl.DataFrame) -> int:
    """按 UTC 合约日分区写入分钟K (merge-upsert on [symbol, datetime])。

    返回写入行数。datetime 按项目契约是北京墙钟 naive, 分区键由
    market_rules.to_contract_date 换算, 不直接取 datetime.date() ——
    后者会把北京 00:00-08:00 的分钟错分到下一个合约日。
    """
    if df is None or df.is_empty():
        return 0
    missing = [c for c in MINUTE_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"crypto 分钟帧缺少必需列: {missing}")

    frame = df.select([
        pl.col("symbol").cast(pl.Utf8),
        pl.col("datetime").cast(pl.Datetime("us")),
        *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "amount")],
    ]).with_columns(
        pl.col("datetime").map_elements(
            lambda dt: to_contract_date(dt, CRYPTO), return_dtype=pl.Date
        ).alias("_contract_day")
    )

    written = 0
    for (day,), part in frame.group_by(["_contract_day"], maintain_order=True):
        payload = part.drop("_contract_day").unique(subset=["symbol", "datetime"], keep="last").sort(
            ["symbol", "datetime"]
        )
        out_dir = _minute_partition_dir(repo.store.data_dir, day)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "part.parquet"
        if out_path.exists():
            try:
                existing = pl.read_parquet(out_path)
                payload = pl.concat([existing, payload], how="diagonal_relaxed").unique(
                    subset=["symbol", "datetime"], keep="last"
                ).sort(["symbol", "datetime"])
            except Exception as e:
                logger.warning("crypto 分钟分区 %s 合并失败, 覆盖写入: %s", day, e)
        atomic_write_parquet(payload, out_path)
        written += payload.height
    return written


def sync_crypto_minute(
    repo,
    symbols: list[str] | None = None,
    contract_day: date | None = None,
) -> int:
    """同步当前合约日的分钟K。返回写入行数。

    窗口取 [合约日 08:00 北京, 下一合约日 08:00 北京) —— 与分区键同源,
    避免 08:00 边界上的分钟重复入库或丢行。
    """
    target = [str(s) for s in (symbols or sorted(repo.get_crypto_symbol_set())) if s]
    if not target:
        logger.warning("crypto 分钟同步跳过: 标的池为空 (需先同步维表)")
        return 0
    day = contract_day or to_contract_date(datetime.now())
    start_time = contract_day_start(day, CRYPTO)
    end_time = contract_day_start(day + timedelta(days=1), CRYPTO) - timedelta(seconds=1)
    df = _provider().get_minute(target, start_time=start_time, end_time=end_time)
    if df.is_empty():
        logger.warning("crypto 分钟同步: 合约日 %s 无数据", day)
        return 0
    written = write_crypto_minute_partitions(repo, df)
    logger.info("crypto 分钟已同步: 合约日 %s, %d 行", day, written)
    return written
