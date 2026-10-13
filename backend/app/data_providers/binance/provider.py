"""币安 USDT 永续合约 provider — 映射到项目内部契约 (crypto 资产类别)。

为什么不是 app/plugins/ 下的数据源插件: 插件与自定义源的 datasets 声明是
**数据集级、全局的** (docs/plugin-development.md「能力声明的粒度」), 声明了
realtime 就会把整个 A 股实时轮询周期一起路由过去。币安只能服务 crypto 资产,
注册成插件会导致用户可把 A 股日K/实时切到它上面 (fail-open)。
本 provider 因此挂在 data_providers 层, 由 crypto 专用注册表按资产类别解析。

口径 (见 app/market_rules):
  - 日归属 = UTC 合约日 (date = K 线 openTime 的 UTC 日期);
  - 分钟 datetime = 北京墙钟 naive (UTC openTime + 8h), 与项目分钟契约一致;
  - volume 保持上游口径 (币/张), 不乘 100 —— crypto 无「手」概念;
  - amount = quoteVolume (USDT 计价);
  - 无复权因子、无涨跌停、无股本。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import polars as pl

from app.market_time import CN_TZ

from .client import BinanceClient, BinanceClientError

logger = logging.getLogger(__name__)

NAME = "binance"
DISPLAY_NAME = "币安 USDT 永续"

# 只收 USDT 本位永续 (交割合约有到期日, 不属于本项目 24x7 语义)
QUOTE_ASSET = "USDT"
CONTRACT_TYPE = "PERPETUAL"

DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
MINUTE_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def utc_ms_to_cn_wallclock(ms: int) -> datetime:
    """UTC 毫秒时间戳 → 北京墙钟 naive (项目分钟 datetime 契约)。"""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).astimezone(CN_TZ).replace(tzinfo=None)


def utc_ms_to_contract_date(ms: int) -> datetime.date:
    """UTC 毫秒时间戳 → UTC 合约日 (K 线口径与交易所一致)。"""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).date()


class BinanceProvider:
    """永续合约行情 provider。方法为软/硬失败混合, 语义见各 docstring。"""

    name = NAME
    display_name = DISPLAY_NAME
    builtin = True

    def __init__(self, client: BinanceClient | None = None) -> None:
        self._client = client or BinanceClient()
        # 与插件契约对齐: 供上层统一读取能力声明 (数据集级)
        self.datasets = ("daily", "minute", "realtime")

    def close(self) -> None:
        """无长连接资源 (httpx.Client 按请求创建), 保持接口对齐。"""

    # ---------------- 维表 ----------------

    def get_crypto_instruments(self) -> list[dict]:
        """返回 USDT 本位永续合约清单 (internal Instrument 形状)。

        网络/结构异常上抛: 维表同步失败必须显式可见, 不能静默留空导致
        已入库标的在下一轮被判定为「未登记 → stock」。
        """
        info = self._client.exchange_info()
        rows: list[dict] = []
        for item in info.get("symbols", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("quoteAsset") != QUOTE_ASSET:
                continue
            if item.get("contractType") != CONTRACT_TYPE:
                continue
            if item.get("status") != "TRADING":
                continue
            symbol = str(item.get("symbol") or "").strip()
            if not symbol:
                continue
            rows.append({
                "symbol": symbol,
                # 上游只给 baseAsset (SOXL), 用 symbol 自身兜底可读名
                "name": str(item.get("baseAsset") or symbol),
                "code": symbol,
                "exchange": "BINANCE",
                "asset_type": "crypto",
                "source": NAME,
            })
        if not rows:
            raise BinanceClientError("exchangeInfo 未解析出任何 USDT 永续合约")
        return rows

    # ---------------- 日K ----------------

    def get_daily(
        self,
        symbols: list[str],
        start_date=None,
        end_date=None,
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """日K (原始价)。date 为 UTC 合约日。单标的失败软跳过, 不阻断其余标的。"""
        start_ms = None
        end_ms = None
        if start_date is not None:
            start_ms = int(datetime.combine(start_date, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
        if end_date is not None:
            end_ms = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC).timestamp() * 1000) - 1

        frames: list[pl.DataFrame] = []
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            try:
                rows = self._client.klines(symbol, "1d", start_ms=start_ms, end_ms=end_ms)
            except BinanceClientError as e:
                logger.warning("日K拉取失败 %s: %s", symbol, e)
                if on_chunk_done:
                    on_chunk_done(index, total)
                continue
            frame = self._klines_to_daily(symbol, rows)
            if not frame.is_empty():
                frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index, total)

        if not frames:
            return pl.DataFrame(schema={c: pl.Utf8 if c == "symbol" else pl.Float64 for c in DAILY_COLS} | {"date": pl.Date})
        return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "date"])

    def _klines_to_daily(self, symbol: str, rows: list[list]) -> pl.DataFrame:
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            out.append({
                "symbol": symbol,
                "date": utc_ms_to_contract_date(int(row[0])),
                "open": _to_float(row[1]),
                "high": _to_float(row[2]),
                "low": _to_float(row[3]),
                "close": _to_float(row[4]),
                "volume": _to_float(row[5]),
                "amount": _to_float(row[7]),
            })
        if not out:
            return pl.DataFrame()
        return pl.DataFrame(out).select([
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date),
            *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "amount")],
        ])

    # ---------------- 分钟K ----------------

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        """分钟K。datetime 为北京墙钟 naive; 入参按北京墙钟解释后换算为 UTC 查询窗口。"""
        start_ms = int(start_time.replace(tzinfo=CN_TZ).timestamp() * 1000) if start_time else None
        end_ms = int(end_time.replace(tzinfo=CN_TZ).timestamp() * 1000) if end_time else None

        frames: list[pl.DataFrame] = []
        for symbol in symbols:
            try:
                rows = self._client.klines(symbol, freq, start_ms=start_ms, end_ms=end_ms)
            except BinanceClientError as e:
                logger.warning("分钟K拉取失败 %s: %s", symbol, e)
                continue
            frame = self._klines_to_minute(symbol, rows)
            if not frame.is_empty():
                frames.append(frame)
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "datetime"])

    def _klines_to_minute(self, symbol: str, rows: list[list]) -> pl.DataFrame:
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            out.append({
                "symbol": symbol,
                "datetime": utc_ms_to_cn_wallclock(int(row[0])),
                "open": _to_float(row[1]),
                "high": _to_float(row[2]),
                "low": _to_float(row[3]),
                "close": _to_float(row[4]),
                "volume": _to_float(row[5]),
                "amount": _to_float(row[7]),
            })
        if not out:
            return pl.DataFrame()
        return pl.DataFrame(out).select([
            pl.col("symbol").cast(pl.Utf8),
            pl.col("datetime").cast(pl.Datetime("us")),
            *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "amount")],
        ])

    # ---------------- 实时快照 ----------------

    def get_realtime(self, symbols: list[str] | None = None) -> list[dict]:
        """合约日实时快照。软失败: 单标的失败跳过, 整体异常返回 []。

        prev_close 取上一合约日收盘 (日K第二根), 而不是 24 小时窗口开盘价 ——
        后者是滚动 24h, 与「当日涨跌幅」口径不符。
        """
        if not symbols:
            return []
        out: list[dict] = []
        for symbol in symbols:
            try:
                rows = self._client.klines(symbol, "1d", limit=2)
            except BinanceClientError as e:
                logger.warning("实时快照拉取失败 %s: %s", symbol, e)
                continue
            if not rows:
                continue
            current = rows[-1]
            prev_close = _to_float(rows[-2][4]) if len(rows) >= 2 else None
            last = _to_float(current[4])
            change_pct = None
            if prev_close and last is not None and prev_close != 0:
                # 入口契约: 小数制 (0.0366 = 3.66%)
                change_pct = (last - prev_close) / prev_close
            out.append({
                "symbol": symbol,
                "name": symbol,
                "last_price": last,
                "prev_close": prev_close,
                "open": _to_float(current[1]),
                "high": _to_float(current[2]),
                "low": _to_float(current[3]),
                "volume": _to_float(current[5]),
                "amount": _to_float(current[7]),
                "change_pct": change_pct,
                "quote_ts": int(current[6]),  # 当前合约日 K 线收盘时间 (UTC ms)
            })
        return out

    def test_dataset(self, dataset: str, symbols=None) -> dict:
        """设置页/自检用的试拉入口。"""
        try:
            if dataset == "daily":
                df = self.get_daily(list(symbols or ["SOXLUSDT"]))
            elif dataset == "minute":
                df = self.get_minute(list(symbols or ["SOXLUSDT"]))
            elif dataset == "realtime":
                rows = self.get_realtime(list(symbols or ["SOXLUSDT"]))
                return {"provider": NAME, "dataset": dataset, "rows": len(rows), "preview": rows[:5]}
            else:
                return {"provider": NAME, "dataset": dataset, "error": f"未支持的数据集: {dataset}"}
        except Exception as e:
            return {"provider": NAME, "dataset": dataset, "error": str(e)}
        return {
            "provider": NAME,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": df.head(5).to_dicts(),
        }
