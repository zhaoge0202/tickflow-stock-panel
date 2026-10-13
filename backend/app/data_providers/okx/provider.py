"""OKX 永续合约 provider — 映射到项目内部契约 (crypto 资产类别)。

口径 (见 app/market_rules, 资产类别规则单一权威):
  - 日归属 = **北京自然日**。这不是我们自选的: OKX 官方日线以 UTC+8 零点切日
    (实测 1D 的 ts = UTC 16:00 = 北京 00:00), 因此「口径与交易所一致」在这里
    恰好等于项目的 CN_TZ 契约, 无需任何时区换算。
  - 分钟 datetime = 北京墙钟 naive (ts + 8h), 与项目分钟契约一致;
  - volume = 上游 vol (张; 该合约 ctVal=1, 1 张 = 1 个标的单位), 不按「手」换算;
  - amount = volCcyQuote (USDT 计价成交额);
  - 无复权、无涨跌停、无股本。
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import polars as pl

from app.market_rules import CRYPTO, to_contract_date
from app.market_time import CN_TZ

from .client import CANDLES_MAX_LIMIT, OkxClient, OkxClientError

logger = logging.getLogger(__name__)

NAME = "okx"
DISPLAY_NAME = "OKX 永续"

INSTRUMENT_TYPE = "SWAP"
# 只收 USDT 本位线性永续 (币本位/交割不属于本项目 24x7 语义)
SETTLE_CCY = "USDT"
CONTRACT_TYPE_LINEAR = "linear"

DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
MINUTE_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

BAR_DAILY = "1D"
BAR_MINUTE = "1m"

# 单标的日K回补上限: 需求窗口最多一年, 取 400 根 (含翻页) 足够
_DAILY_MAX_BARS = 400

# 各 bar 的分钟数, 用于按窗口长度折算需要多少根 K 线
_BAR_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}
# 分钟K单次同步的翻页上限 (1440 根/合约日 + 容差; history-candles 每页 100)
_MINUTE_MAX_BARS = 1500


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bars_needed(bar: str, start_ms: int | None, end_ms: int | None) -> int:
    """按请求窗口折算需要多少根 K 线, 决定翻页深度。

    24x7 的一个合约日有 1440 根 1m, 若沿用单页上限 (300) 会永远只同步到约
    [当前-5h, 当前] 这一段, 当日分钟永远判不完整 (静默少数据)。
    """
    if start_ms is None:
        # 未给窗口: 只要最新一段 (实时/巡检场景)
        return CANDLES_MAX_LIMIT
    end = end_ms if end_ms is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
    minutes = max(1.0, (end - start_ms) / 60000.0)
    per_bar = _BAR_MINUTES.get(bar, 1)
    return max(1, min(_MINUTE_MAX_BARS, int(minutes / per_bar) + 2))


def okx_ms_to_cn_wallclock(ms: int) -> datetime:
    """OKX 毫秒时间戳 (UTC) → 北京墙钟 naive。"""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).astimezone(CN_TZ).replace(tzinfo=None)


def okx_ms_to_contract_date(ms: int) -> date:
    """OKX 毫秒时间戳 (UTC) → 合约日 (北京自然日, 与官方日线切日一致)。"""
    return to_contract_date(okx_ms_to_cn_wallclock(ms), CRYPTO)


class OkxProvider:
    """永续合约行情 provider。方法为软/硬失败混合, 语义见各 docstring。"""

    name = NAME
    display_name = DISPLAY_NAME
    builtin = True

    def __init__(self, client: OkxClient | None = None) -> None:
        self._client = client or OkxClient()
        self.datasets = ("daily", "minute", "realtime")

    def close(self) -> None:
        """无长连接资源 (httpx.Client 按请求创建), 保持接口对齐。"""

    # ---------------- 维表 ----------------

    def get_crypto_instruments(self) -> list[dict]:
        """返回 USDT 本位线性永续清单 (internal Instrument 形状)。

        网络/结构异常上抛: 维表同步失败必须显式可见, 不能静默留空 ——
        否则已入库标的会在下一轮被判成「未登记 → stock」而吃满 A 股规则。
        """
        rows: list[dict] = []
        for item in self._client.instruments(INSTRUMENT_TYPE):
            if not isinstance(item, dict):
                continue
            if item.get("settleCcy") != SETTLE_CCY:
                continue
            if item.get("ctType") != CONTRACT_TYPE_LINEAR:
                continue
            if item.get("state") != "live":
                continue
            # 客户端按 instType=SWAP 取数, 这里再挡一次: 上游混入交割合约时必须剔除,
            # 否则带了到期日的合约会以 24x7 语义入库。
            if str(item.get("instType") or "").upper() != INSTRUMENT_TYPE:
                continue
            symbol = str(item.get("instId") or "").strip().upper()
            if not symbol:
                continue
            rows.append({
                "symbol": symbol,
                # OKX 无友好名: 用标的族 (SOXL-USDT) 兜底可读名
                "name": str(item.get("instFamily") or symbol),
                "code": symbol,
                "exchange": "OKX",
                "asset_type": "crypto",
                "source": NAME,
            })
        if not rows:
            raise OkxClientError("OKX instruments 未解析出任何 USDT 线性永续")
        return rows

    # ---------------- K 线取数 ----------------

    def _fetch_bars(self, symbol: str, bar: str, need: int, oldest_ms: int | None) -> list[list]:
        """取 [oldest_ms, now] 的 K 线, 新→旧返回。近端用 candles, 更早用 history-candles 翻页。"""
        rows = self._client.candles(symbol, bar, limit=need)
        if oldest_ms is None:
            return rows
        while rows:
            oldest = min(int(r[0]) for r in rows if r and len(r) > 1)
            if oldest <= oldest_ms:
                break
            page = self._client.history_candles(symbol, bar, after_ms=oldest)
            if not page:
                break
            # 翻页可能返回重叠的一根 (after 为开区间起点), 去重后拼接
            seen = {int(r[0]) for r in rows}
            fresh = [r for r in page if int(r[0]) not in seen]
            if not fresh:
                break
            rows.extend(fresh)
            self._client.sleep_for_rate_limit()
            if len(rows) >= need:
                break
        return rows

    def _bars_to_daily(self, symbol: str, rows: list[list], start_date: date | None, end_date: date | None) -> pl.DataFrame:
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            day = okx_ms_to_contract_date(int(row[0]))
            if start_date is not None and day < start_date:
                continue
            if end_date is not None and day > end_date:
                continue
            out.append({
                "symbol": symbol,
                "date": day,
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
        ]).unique(subset=["symbol", "date"], keep="last").sort(["symbol", "date"])

    def get_daily(
        self,
        symbols: list[str],
        start_date: date | None = None,
        end_date: date | None = None,
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """日K (原始价)。date 为北京自然日 (与 OKX 官方日线切日一致)。单标的失败软跳过。"""
        start_ms = None
        if start_date is not None:
            start_ms = int(datetime.combine(start_date, datetime.min.time(), tzinfo=CN_TZ).timestamp() * 1000)

        frames: list[pl.DataFrame] = []
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            try:
                rows = self._fetch_bars(symbol, BAR_DAILY, _DAILY_MAX_BARS, start_ms)
            except OkxClientError as e:
                logger.warning("日K拉取失败 %s: %s", symbol, e)
                if on_chunk_done:
                    on_chunk_done(index, total)
                continue
            frame = self._bars_to_daily(symbol, rows, start_date, end_date)
            if not frame.is_empty():
                frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index, total)

        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "date"])

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        freq: str = BAR_MINUTE,
    ) -> pl.DataFrame:
        """分钟K。datetime 为北京墙钟 naive; 入参按北京墙钟解释。"""
        oldest_ms = int(start_time.replace(tzinfo=CN_TZ).timestamp() * 1000) if start_time else None
        end_ms = int(end_time.replace(tzinfo=CN_TZ).timestamp() * 1000) if end_time else None
        need = _bars_needed(freq, oldest_ms, end_ms)

        frames: list[pl.DataFrame] = []
        for symbol in symbols:
            try:
                rows = self._fetch_bars(symbol, freq, need, oldest_ms)
            except OkxClientError as e:
                logger.warning("分钟K拉取失败 %s: %s", symbol, e)
                continue
            frame = self._bars_to_minute(symbol, rows, end_ms)
            if not frame.is_empty():
                frames.append(frame)
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "datetime"])

    def _bars_to_minute(self, symbol: str, rows: list[list], end_ms: int | None) -> pl.DataFrame:
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            ts = int(row[0])
            if end_ms is not None and ts > end_ms:
                continue
            out.append({
                "symbol": symbol,
                "datetime": okx_ms_to_cn_wallclock(ts),
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
        ]).unique(subset=["symbol", "datetime"], keep="last").sort(["symbol", "datetime"])

    # ---------------- 实时快照 ----------------

    def get_realtime(self, symbols: list[str] | None = None) -> list[dict]:
        """当前合约日实时快照。软失败: 单标的失败跳过, 无 symbols 返回 []。

        prev_close 取上一合约日收盘 (日K倒数第二根完成 bar), 而不是 24h 滚动开盘价 ——
        后者与「当日涨跌幅」口径不符。
        """
        if not symbols:
            return []
        out: list[dict] = []
        for symbol in symbols:
            try:
                rows = self._client.candles(symbol, BAR_DAILY, limit=3)
            except OkxClientError as e:
                logger.warning("实时快照拉取失败 %s: %s", symbol, e)
                continue
            closed = [r for r in rows if len(r) > 8 and str(r[8]) == "1"]
            if not rows:
                continue
            current = rows[0]
            prev_close = _to_float(closed[0][4]) if closed else None
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
                "quote_ts": int(current[0]),
            })
        return out

    def test_dataset(self, dataset: str, symbols=None) -> dict:
        """设置页/自检用的试拉入口。"""
        try:
            if dataset == "daily":
                df = self.get_daily(list(symbols or ["SOXL-USDT-SWAP"]))
            elif dataset == "minute":
                df = self.get_minute(list(symbols or ["SOXL-USDT-SWAP"]))
            elif dataset == "realtime":
                rows = self.get_realtime(list(symbols or ["SOXL-USDT-SWAP"]))
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
