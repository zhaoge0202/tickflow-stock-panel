"""tdx-api sidecar provider.

通过 HTTP 调用本地/旁路 tdx-api 服务, 并归一化为本项目内部数据契约。
tdx-api 自身负责通达信 TCP 连接、SOCKS5 代理池和 IP 池。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.services.symbols import guess_exchange_suffix
from app.tickflow.rate_limits import chunked

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "minute", "realtime", "trade_ticks")
_DEFAULT_BASE_URL = "http://127.0.0.1:8080"
_QUOTE_BATCH = 50
_KLINE_BATCH = 8
_CN_TZ = ZoneInfo("Asia/Shanghai")
_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


@dataclass
class _TDXAPIConfig:
    """轻量 config shim, 让 custom loader 能识别内置 provider 能力。"""

    name: str = "tdxapi"
    display_name: str = "tdx-api(通达信代理池)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class TDXAPIProvider:
    """基于 tdx-api sidecar 的内置行情源。"""

    name = "tdxapi"
    builtin = True

    def __init__(self) -> None:
        self.config = _TDXAPIConfig()
        self.base_url = _base_url()
        self.timeout = float(os.getenv("TDX_API_TIMEOUT", "30") or 30)
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        self._instrument_cache: list[dict] | None = None

    def close(self) -> None:
        self._client.close()

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _KLINE_BATCH)
        for i, chunk in enumerate(chunks):
            for symbol in chunk:
                app_symbol = _to_app_symbol(symbol, None) or str(symbol).upper()
                try:
                    rows = self._fetch_kline_rows(symbol, "day")
                except Exception as e:
                    logger.warning("tdx-api daily 拉取失败(%s): %s", app_symbol, e)
                    continue
                mapped = [
                    row for row in (self._daily_row(app_symbol, item) for item in rows)
                    if row is not None and _in_date_range(row["date"], start_time, end_time)
                ]
                if mapped:
                    df = normalize_daily(mapped, source=self.name)
                    if not df.is_empty():
                        frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        kline_type = _minute_type(freq)
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _KLINE_BATCH)
        for i, chunk in enumerate(chunks):
            for symbol in chunk:
                app_symbol = _to_app_symbol(symbol, None) or str(symbol).upper()
                try:
                    rows = self._fetch_kline_rows(symbol, kline_type)
                except Exception as e:
                    logger.warning("tdx-api minute 拉取失败(%s): %s", app_symbol, e)
                    continue
                mapped = [
                    row for row in (self._minute_row(app_symbol, item) for item in rows)
                    if row is not None and _in_datetime_range(row["datetime"], start_time, end_time)
                ]
                if mapped:
                    frames.append(pl.DataFrame(mapped).select(_MINUTE_CANONICAL))
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- realtime ----
    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        target_symbols = symbols or _symbols_from_env()
        if not target_symbols:
            try:
                target_symbols = [item["symbol"] for item in self.get_instruments("stock")]
            except Exception as e:
                logger.warning("tdx-api codes 拉取失败, 无法生成全市场实时列表: %s", e)
                return []
        if not target_symbols:
            return []

        try:
            name_map = {item["symbol"]: item.get("name") for item in self.get_instruments("stock")}
        except Exception as e:
            logger.warning("tdx-api codes 拉取失败, 实时行情将不补名称: %s", e)
            name_map = {}
        rows: list[dict] = []
        for chunk in chunked(target_symbols, _QUOTE_BATCH):
            codes = [_to_tdx_code(symbol) for symbol in chunk]
            try:
                data = self._request("POST", "/api/batch-quote", json={"codes": codes})
            except Exception as e:
                logger.warning("tdx-api realtime batch 拉取失败(%d symbols): %s", len(chunk), e)
                continue
            for item in data or []:
                row = self._quote_row(item, name_map)
                if row:
                    rows.append(row)
        return rows

    # ---- trade ticks ----
    def get_trade_ticks(
        self,
        symbol: str,
        trade_date: date | datetime | str | None = None,
        mode: str = "recent",
        limit: int | None = 500,
    ) -> list[dict]:
        """返回单票逐笔成交明细。

        mode:
          - recent: tdx-api /api/trade, 今日最近约 1800 条; 传 date 时取历史某日。
          - all:    tdx-api /api/minute-trade-all, 今日/指定日全量分时成交。
        """
        app_symbol = _to_app_symbol(symbol, None) or str(symbol).upper()
        params = {"code": _to_tdx_code(symbol)}
        date_arg = _tdx_date_arg(trade_date)
        if date_arg:
            params["date"] = date_arg

        path = "/api/minute-trade-all" if str(mode or "").lower() == "all" else "/api/trade"
        data = self._request("GET", path, params=params, timeout=max(self.timeout, 60.0))
        raw_rows = list((data or {}).get("List") or [])

        rows = [
            row for row in (
                self._trade_tick_row(app_symbol, item, seq_in_day=i + 1)
                for i, item in enumerate(raw_rows)
            )
            if row is not None
        ]
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    # ---- instruments ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        if asset_type != "stock":
            return []
        if self._instrument_cache is not None:
            return self._instrument_cache
        data = self._request("GET", "/api/codes", params={"exchange": "all"})
        out: list[dict] = []
        for item in (data or {}).get("codes") or []:
            symbol = _to_app_symbol(item.get("code"), item.get("exchange"))
            if not symbol:
                continue
            exchange = symbol.split(".")[-1]
            out.append({
                "symbol": symbol,
                "name": item.get("name") or symbol,
                "code": item.get("code"),
                "exchange": exchange,
                "region": "CN",
                "type": "stock",
                "ext": {},
            })
        self._instrument_cache = out
        return out

    # ---- settings test ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        symbols = symbols or ["600519.SH"]
        if dataset == "daily":
            df = self.get_daily(symbols[:1], None, None)
            return _preview(self.name, "daily", df)
        if dataset == "minute":
            df = self.get_minute(symbols[:1], None, None)
            return _preview(self.name, "minute", df)
        if dataset == "realtime":
            rows = self.get_realtime(symbols=symbols[: min(len(symbols), _QUOTE_BATCH)])
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        if dataset == "trade_ticks":
            rows = self.get_trade_ticks(symbols[0], mode="recent", limit=5)
            return {
                "provider": self.name,
                "dataset": "trade_ticks",
                "rows": len(rows),
                "columns": list(rows[0].keys()) if rows else [],
                "preview": rows[:5],
            }
        raise ValueError(f"tdx-api 不支持数据集: {dataset}")

    def _fetch_kline_rows(self, symbol: str, kline_type: str) -> list[dict]:
        data = self._request(
            "GET",
            "/api/kline-all/tdx",
            params={"code": _to_tdx_code(symbol), "type": kline_type},
            timeout=max(self.timeout, 60.0),
        )
        return list((data or {}).get("list") or [])

    def _request(self, method: str, path: str, **kwargs):
        resp = self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and "code" in payload:
            if payload.get("code") != 0:
                raise RuntimeError(str(payload.get("message") or "tdx-api request failed"))
            return payload.get("data")
        return payload

    @staticmethod
    def _daily_row(symbol: str, item: dict) -> dict | None:
        dt = _parse_tdx_time(item.get("Time"))
        if dt is None:
            return None
        return {
            "symbol": symbol,
            "date": dt.date(),
            "open": _price(item.get("Open")),
            "high": _price(item.get("High")),
            "low": _price(item.get("Low")),
            "close": _price(item.get("Close")),
            "volume": _volume(item.get("Volume")),
            "amount": _price(item.get("Amount")),
        }

    @staticmethod
    def _minute_row(symbol: str, item: dict) -> dict | None:
        dt = _parse_tdx_time(item.get("Time"))
        if dt is None:
            return None
        return {
            "symbol": symbol,
            "datetime": dt.replace(tzinfo=None),
            "open": _price(item.get("Open")),
            "high": _price(item.get("High")),
            "low": _price(item.get("Low")),
            "close": _price(item.get("Close")),
            "volume": _volume(item.get("Volume")),
            "amount": _price(item.get("Amount")),
        }

    @staticmethod
    def _quote_row(item: dict, name_map: dict[str, str | None]) -> dict | None:
        symbol = _to_app_symbol(item.get("Code"), item.get("Exchange"))
        if not symbol:
            return None
        k = item.get("K") or {}
        prev_close = _price(k.get("Last"))
        trade_price = _price(k.get("Close"))
        auction = _auction_fields(item, prev_close)
        is_auction_reference = trade_price <= 0 and auction.get("auction_price") is not None
        last_price = auction["auction_price"] if is_auction_reference else trade_price
        high = _price(k.get("High"))
        low = _price(k.get("Low"))
        change_amount = last_price - prev_close if prev_close else 0.0
        change_pct = change_amount / prev_close if prev_close else 0.0
        amplitude = (high - low) / prev_close if prev_close else 0.0
        row = {
            "symbol": symbol,
            "name": name_map.get(symbol),
            "source": "tdxapi",
            "last_price": last_price,
            "prev_close": prev_close,
            "open": _price(k.get("Open")),
            "high": high,
            "low": low,
            "volume": _volume(item.get("TotalHand")),
            # 集合竞价阶段 Amount 常见为极小浮点噪声,不能当成交额使用。
            "amount": None if is_auction_reference else _float(item.get("Amount")),
            "change_pct": change_pct,
            "change_amount": change_amount,
            "amplitude": amplitude,
            "turnover_rate": None,
            "timestamp": _server_time_ms(item.get("ServerTime")),
        }
        row.update(_best_level_fields(item))
        if is_auction_reference:
            row.update({
                "price_type": "auction_reference",
                "market_phase": "preopen_auction",
                **auction,
            })
        else:
            row["price_type"] = "trade"
        return row

    @staticmethod
    def _trade_tick_row(symbol: str, item: dict, seq_in_day: int) -> dict | None:
        dt = _parse_tdx_time(item.get("Time"))
        if dt is None:
            return None
        price = _price(item.get("Price"))
        volume = int(_float(item.get("Volume")))
        raw_status = _int_or_none(item.get("Status"))
        side, side_label = _trade_side(raw_status)
        return {
            "symbol": symbol,
            "trade_date": dt.date(),
            "datetime": dt.replace(tzinfo=None),
            "seq_in_day": int(seq_in_day),
            "price": price,
            "volume": volume,
            "amount": price * volume * 100.0,
            "side": side,
            "side_label": side_label,
            "order_count": _int_or_none(item.get("Number")),
            "raw_status": raw_status,
            "source": "tdxapi",
        }


def _best_level_fields(item: dict) -> dict:
    """从 TDX 五档快照取一档价格/量。

    集合竞价期间 TDX 会把虚拟参考价放在买一/卖一同价位置, 所以这里
    只做原始字段透传, 业务语义由 _auction_fields 单独识别。
    """
    buy1 = _level(item.get("BuyLevel"), 0)
    sell1 = _level(item.get("SellLevel"), 0)
    return {
        "bid1": _price_or_none(buy1.get("Price")),
        "ask1": _price_or_none(sell1.get("Price")),
        "bid1_vol": _int_or_none(buy1.get("Number")),
        "ask1_vol": _int_or_none(sell1.get("Number")),
    }


def _auction_fields(item: dict, prev_close: float | None) -> dict:
    """识别 TDX 集合竞价参考价。

    实测 9:15-9:25:
      - K.Close/Open/High/Low 为 0;
      - BuyLevel[0] 与 SellLevel[0] 同价同量, 对应虚拟参考价/匹配量;
      - 第二档只有一侧有量, 对应虚拟未匹配量方向。
    """
    buy1 = _level(item.get("BuyLevel"), 0)
    sell1 = _level(item.get("SellLevel"), 0)
    buy2 = _level(item.get("BuyLevel"), 1)
    sell2 = _level(item.get("SellLevel"), 1)
    buy_price = _price_or_none(buy1.get("Price"))
    sell_price = _price_or_none(sell1.get("Price"))
    buy_vol = _int_or_none(buy1.get("Number"))
    sell_vol = _int_or_none(sell1.get("Number"))
    if (
        buy_price is None
        or sell_price is None
        or buy_price <= 0
        or sell_price <= 0
        or abs(buy_price - sell_price) > 1e-9
        or buy_vol != sell_vol
    ):
        return {}

    unmatched_buy = _int_or_none(buy2.get("Number")) or 0
    unmatched_sell = _int_or_none(sell2.get("Number")) or 0
    unmatched_side = None
    unmatched_volume = 0
    if unmatched_buy > unmatched_sell:
        unmatched_side = "buy"
        unmatched_volume = unmatched_buy
    elif unmatched_sell > unmatched_buy:
        unmatched_side = "sell"
        unmatched_volume = unmatched_sell

    change_pct = (buy_price - prev_close) / prev_close if prev_close else None
    return {
        "auction_price": buy_price,
        "auction_matched_volume": buy_vol,
        "auction_unmatched_side": unmatched_side,
        "auction_unmatched_volume": unmatched_volume,
        "auction_change_pct": change_pct,
    }


def _level(levels, index: int) -> dict:
    if isinstance(levels, list) and len(levels) > index and isinstance(levels[index], dict):
        return levels[index]
    return {}


def availability() -> tuple[bool, str]:
    base_url = _base_url()
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            resp = client.get("/api/health")
            resp.raise_for_status()
            data = resp.json()
            quote_resp = client.post("/api/batch-quote", json={"codes": ["002491"]})
            quote_resp.raise_for_status()
            quote_data = quote_resp.json()
    except Exception as e:
        return False, f"tdx-api 不可用({base_url}): {e}"
    if data.get("status") != "healthy":
        return False, f"tdx-api health 异常: {data}"
    if not (quote_data.get("data") or []):
        return False, "tdx-api quote 检测无数据"
    return True, f"ok ({base_url})"


def _base_url() -> str:
    return (os.getenv("TDX_API_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _symbols_from_env() -> list[str]:
    raw = os.getenv("TDX_API_REALTIME_SYMBOLS", "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _to_tdx_code(symbol: str | None) -> str:
    text = str(symbol or "").strip()
    if "." in text:
        code, suffix = text.split(".", 1)
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix.upper())
        return f"{prefix}{code}" if prefix else code
    if len(text) == 8 and text[:2].lower() in {"sh", "sz", "bj"}:
        return text.lower()
    if len(text) == 6 and text.isdigit():
        suffix = guess_exchange_suffix(text)
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(str(suffix or "").upper())
        return f"{prefix}{text}" if prefix else text
    return text


def _tdx_date_arg(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return None if value.date() == _cn_today() else value.strftime("%Y%m%d")
    if isinstance(value, date):
        return None if value == _cn_today() else value.strftime("%Y%m%d")
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("-", "")
    if len(normalized) == 8 and normalized.isdigit():
        try:
            if date.fromisoformat(f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}") == _cn_today():
                return None
        except ValueError:
            pass
    return normalized


def _cn_today() -> date:
    return datetime.now(_CN_TZ).date()


def _to_app_symbol(code: str | None, exchange) -> str | None:
    raw_code = str(code or "").strip()
    if not raw_code:
        return None
    if "." in raw_code:
        return raw_code.upper()
    if len(raw_code) == 8 and raw_code[:2].lower() in {"sh", "sz", "bj"}:
        exchange = raw_code[:2]
        raw_code = raw_code[2:]
    suffix = _exchange_suffix(exchange, raw_code)
    return f"{raw_code}.{suffix}" if suffix else None


def _exchange_suffix(exchange, code: str | None = None) -> str | None:
    if isinstance(exchange, str):
        value = exchange.lower()
        if value in {"sh", "1"}:
            return "SH"
        if value in {"sz", "0"}:
            return "SZ"
        if value in {"bj", "2"}:
            return "BJ"
    if isinstance(exchange, int):
        return {0: "SZ", 1: "SH", 2: "BJ"}.get(exchange)
    text = str(code or "")
    if text.startswith(("5", "6")):
        return "SH"
    if text.startswith(("0", "1", "3")):
        return "SZ"
    if text.startswith(("8", "43", "92")):
        return "BJ"
    return None


def _minute_type(freq: str) -> str:
    minutes = "".join(ch for ch in str(freq or "1m") if ch.isdigit()) or "1"
    return {
        "1": "minute1",
        "5": "minute5",
        "15": "minute15",
        "30": "minute30",
        "60": "hour",
    }.get(minutes, "minute1")


def _parse_tdx_time(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_CN_TZ).replace(tzinfo=None)
    return dt


def _in_date_range(value, start_time: datetime | None, end_time: datetime | None) -> bool:
    if start_time and value < start_time.date():
        return False
    return not (end_time and value > end_time.date())


def _in_datetime_range(value: datetime, start_time: datetime | None, end_time: datetime | None) -> bool:
    if start_time and value < start_time.replace(tzinfo=None):
        return False
    return not (end_time and value > end_time.replace(tzinfo=None))


def _price(value) -> float:
    return _float(value) / 1000.0


def _price_or_none(value) -> float | None:
    price = _price(value)
    return price if price > 0 else None


def _volume(value) -> float:
    return _float(value)


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trade_side(raw_status: int | None) -> tuple[str, str]:
    if raw_status == 0:
        return "buy", "主买"
    if raw_status == 1:
        return "sell", "主卖"
    if raw_status == 2:
        return "neutral", "中性"
    return "unknown", "未知"


def _server_time_ms(value) -> int | None:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None

    now = datetime.now(_CN_TZ)
    now_ms = int(now.timestamp() * 1000)
    max_future_ms = now_ms + 24 * 60 * 60 * 1000

    if 946_684_800_000 <= raw <= max_future_ms:
        return raw
    if 946_684_800 <= raw <= max_future_ms // 1000:
        return raw * 1000

    return _tdx_compact_time_ms(raw, now=now)


def _tdx_compact_time_ms(value: int, *, now: datetime) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    head = text[:-6]
    try:
        hour = int(head) if head else 0
        tail = text[-6:].zfill(6)
        marker = int(tail[:2])
        if marker < 60:
            minute = marker
            second = int(tail[2:]) * 60 / 10_000.0
        else:
            raw_tail = int(tail)
            minute = int(raw_tail * 60 / 1_000_000)
            second = ((raw_tail * 60) % 1_000_000) * 60 / 1_000_000.0
    except ValueError:
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second < 60):
        return None

    midnight = now.astimezone(_CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    decoded = midnight + timedelta(hours=hour, minutes=minute, seconds=second)
    return int(decoded.timestamp() * 1000)


def _preview(provider: str, dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": provider,
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
