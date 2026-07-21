"""tdx-api sidecar provider.

通过 HTTP 调用本地/旁路 tdx-api 服务, 并归一化为本项目内部数据契约。
tdx-api 自身负责通达信 TCP 连接、SOCKS5 代理池和 IP 池。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.services.symbols import guess_exchange_suffix
from app.tickflow.rate_limits import chunked

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "minute", "realtime", "trade_ticks", "financial")
_DEFAULT_BASE_URL = "http://127.0.0.1:8080"
_QUOTE_BATCH = 50
_KLINE_BATCH = 8
_MINUTE_FETCH_ATTEMPTS = 3
_CN_TZ = ZoneInfo("Asia/Shanghai")
_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
# TDX /api/finance 只有单票财务快照(~37 字段), 不是完整三大表历史。
# 这里同时保留原始映射, 并在 _financial_row 中派生前端契约字段
# (eps_basic / revenue / period_end 等), 避免页面大面积显示 "—"。
_FINANCIAL_TABLE_FIELDS: dict[str, dict[str, str]] = {
    "metrics": {
        "market": "Market",
        "code": "Code",
        "float_shares": "LiuTongGuBen",
        "total_shares": "ZongGuBen",
        "province_code": "Province",
        "industry_code": "Industry",
        "shareholder_count": "GuDongRenShu",
        "total_assets": "ZongZiChan",
        "net_assets": "JingZiChan",
        "main_revenue": "ZhuYingShouRu",
        "operating_profit": "YingYeLiRun",
        "total_profit": "LiRunZongHe",
        "net_profit": "JingLiRun",
        "operating_cash_flow": "JingYingXianJinLiu",
        "total_cash_flow": "ZongXianJinLiu",
    },
    "income": {
        "main_revenue": "ZhuYingShouRu",
        "main_profit": "ZhuYingLiRun",
        "operating_profit": "YingYeLiRun",
        "investment_income": "TouZiShouYi",
        "total_profit": "LiRunZongHe",
        "profit_after_tax": "ShuiHouLiRun",
        "net_profit": "JingLiRun",
        "retained_profit": "WeiFenLiRun",
    },
    "balance_sheet": {
        "float_shares": "LiuTongGuBen",
        "total_shares": "ZongGuBen",
        "total_assets": "ZongZiChan",
        "current_assets": "LiuDongZiChan",
        "fixed_assets": "GuDingZiChan",
        "intangible_assets": "WuXingZiChan",
        "accounts_receivable": "YingShouZhangKuan",
        "inventory": "CunHuo",
        "current_liabilities": "LiuDongFuZhai",
        "long_term_liabilities": "ChangQiFuZhai",
        "capital_reserve": "ZiBenGongJiJin",
        "net_assets": "JingZiChan",
    },
    "cash_flow": {
        "operating_cash_flow": "JingYingXianJinLiu",
        "total_cash_flow": "ZongXianJinLiu",
    },
}
_CORE_INDEXES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000016.SH": "上证50",
}


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
        self._instrument_cache: dict[str, list[dict]] = {}

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
                    rows = self._fetch_minute_rows_with_retry(symbol, kline_type)
                except Exception as e:
                    logger.warning("tdx-api minute 拉取失败(%s): %s", app_symbol, e)
                    if len(symbols) == 1:
                        raise
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

    def _fetch_minute_rows_with_retry(self, symbol: str, kline_type: str) -> list[dict]:
        """TDX 节点会偶发返回业务超时, 换节点重试可恢复。"""
        last_error: Exception | None = None
        for attempt in range(1, _MINUTE_FETCH_ATTEMPTS + 1):
            try:
                return self._fetch_kline_rows(symbol, kline_type)
            except Exception as e:
                last_error = e
                logger.warning(
                    "tdx-api minute 请求失败(%s), 尝试 %d/%d: %s",
                    symbol,
                    attempt,
                    _MINUTE_FETCH_ATTEMPTS,
                    e,
                )
        raise RuntimeError(
            f"TDX 分钟K请求失败, 重试 {_MINUTE_FETCH_ATTEMPTS} 次仍未恢复: {last_error}"
        ) from last_error

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
            rows.extend(self._fetch_realtime_chunk(chunk, name_map))
        return rows

    def _fetch_realtime_chunk(
        self,
        symbols: list[str],
        name_map: dict[str, str | None],
    ) -> list[dict]:
        if not symbols:
            return []
        codes = [_to_tdx_code(symbol) for symbol in symbols]
        try:
            data = self._request("POST", "/api/batch-quote", json={"codes": codes})
        except Exception as e:
            missing_code = _missing_quote_code(e)
            if missing_code:
                remaining = [
                    symbol
                    for symbol, code in zip(symbols, codes, strict=True)
                    if code.lower() != missing_code.lower()
                ]
                if len(remaining) < len(symbols):
                    logger.warning(
                        "tdx-api 跳过失效实时行情代码 %s, 重试剩余 %d 只",
                        missing_code,
                        len(remaining),
                    )
                    return self._fetch_realtime_chunk(remaining, name_map)
            if len(symbols) > 1:
                middle = len(symbols) // 2
                logger.warning(
                    "tdx-api realtime batch 拉取失败(%d symbols), 拆分重试: %s",
                    len(symbols),
                    e,
                )
                return [
                    *self._fetch_realtime_chunk(symbols[:middle], name_map),
                    *self._fetch_realtime_chunk(symbols[middle:], name_map),
                ]
            logger.warning("tdx-api realtime 拉取失败(%s): %s", codes[0], e)
            return []

        rows: list[dict] = []
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

    def get_trade_history_full(
        self,
        symbol: str,
        before: date | datetime | str | None = None,
        limit: int | None = None,
        *,
        start_date: date | datetime | str | None = None,
        end_date: date | datetime | str | None = None,
        include_today: bool = False,
    ) -> list[dict]:
        """返回 `/api/trade-history/full` 的分钟精度历史分笔。

        sidecar 该接口返回 `data.list`, 且价格已经是正常小数价格; 不复用
        `/api/trade` 的千分价解析, 避免二次缩放。
        """
        app_symbol = _to_app_symbol(symbol, None) or str(symbol).upper()
        params: dict[str, str | int | bool] = {"code": _to_tdx_code(symbol)}
        if start_date is not None:
            params["start_date"] = _history_date_arg(start_date)
        if end_date is not None:
            params["end_date"] = _history_date_arg(end_date)
        elif before is not None:
            params["before"] = _history_date_arg(before)
        if limit and limit > 0:
            params["limit"] = int(limit)
        if include_today:
            params["include_today"] = "true"

        data = self._request("GET", "/api/trade-history/full", params=params, timeout=max(self.timeout, 90.0))
        raw_rows = list((data or {}).get("list") or (data or {}).get("List") or [])
        rows = [
            row for row in (
                self._trade_history_tick_row(app_symbol, item, seq_in_day=i + 1)
                for i, item in enumerate(raw_rows)
            )
            if row is not None
        ]
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    # ---- financial ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取通达信财务/基本面快照。

        TDX 该接口是单票当前快照, 不是完整历史财报。这里按项目的四张
        financial 表拆分字段, 便于绕过 TickFlow Expert 门槛做基础分析。
        """
        table = str(table or "").strip().lower()
        if table not in _FINANCIAL_TABLE_FIELDS:
            raise ValueError(f"tdx-api 不支持财务表: {table}")
        if not symbols:
            return pl.DataFrame()

        rows: list[dict] = []
        for symbol in symbols:
            app_symbol = _to_app_symbol(symbol, None) or str(symbol).upper()
            try:
                data = self._request(
                    "GET",
                    "/api/finance",
                    params={"code": _to_tdx_code(symbol)},
                    timeout=max(self.timeout, 60.0),
                )
            except Exception as e:
                logger.warning("tdx-api financial 拉取失败(%s/%s): %s", table, app_symbol, e)
                continue
            row = _financial_row(table, app_symbol, data or {})
            if row:
                rows.append(row)

        return pl.DataFrame(rows) if rows else pl.DataFrame()

    # ---- instruments ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        asset_type = str(asset_type or "stock").lower()
        if asset_type in self._instrument_cache:
            return self._instrument_cache[asset_type]
        if asset_type == "etf":
            out = self._etf_instruments()
            self._instrument_cache[asset_type] = out
            return out
        if asset_type == "index":
            out = self._index_instruments()
            self._instrument_cache[asset_type] = out
            return out
        if asset_type != "stock":
            return []
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
        self._instrument_cache[asset_type] = out
        return out

    def get_market_breadth(self, major_symbols: list[str] | None = None) -> dict:
        """读取 TDX 市场广度和核心指数快照。"""
        data = self._request("GET", "/api/market-stats")
        exchanges = {
            key: _market_exchange_stats((data or {}).get(key) or {})
            for key in ("sh", "sz", "bj")
        }
        up_count = sum(item["up"] for item in exchanges.values())
        down_count = sum(item["down"] for item in exchanges.values())
        flat_count = sum(item["flat"] for item in exchanges.values())
        total_count = sum(item["total"] for item in exchanges.values())
        up_down_ratio = up_count / down_count if down_count > 0 else None
        indices = self._major_index_snapshots(major_symbols or list(_CORE_INDEXES))
        major_change = indices[0].get("change_pct") if indices else None
        now_ms = int(datetime.now(_CN_TZ).timestamp() * 1000)
        return {
            "source": "tdxapi",
            "event_ts": _parse_update_time_ms((data or {}).get("update_time")) or now_ms,
            "ingest_ts": now_ms,
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": flat_count,
            "total_count": total_count,
            "up_down_ratio": up_down_ratio,
            "market_temperature": _market_temperature(up_count, down_count, total_count),
            "major_index_change_pct": major_change,
            "major_indices": indices,
            "exchanges": exchanges,
            "raw": data,
        }

    def _etf_instruments(self) -> list[dict]:
        try:
            data = self._request("GET", "/api/etf", params={"exchange": "all"})
            items = (data or {}).get("list") or []
            out = []
            for item in items:
                symbol = _to_app_symbol(item.get("code"), item.get("exchange"))
                if not symbol:
                    continue
                exchange = symbol.split(".")[-1]
                out.append({
                    "symbol": symbol,
                    "name": item.get("name") or symbol,
                    "code": symbol.split(".")[0],
                    "exchange": exchange,
                    "region": "CN",
                    "type": "etf",
                    "asset_type": "etf",
                    "ext": {"source": "tdxapi", "last_price": item.get("last_price")},
                })
            if out:
                return _unique_instruments(out)
        except Exception as e:
            logger.debug("tdx-api ETF 列表拉取失败,回退 etf-codes: %s", e)

        data = self._request("GET", "/api/etf-codes", params={"prefix": "true"})
        out = []
        for code in (data or {}).get("list") or []:
            symbol = _to_app_symbol(code, None)
            if not symbol:
                continue
            exchange = symbol.split(".")[-1]
            out.append({
                "symbol": symbol,
                "name": symbol,
                "code": symbol.split(".")[0],
                "exchange": exchange,
                "region": "CN",
                "type": "etf",
                "asset_type": "etf",
                "ext": {"source": "tdxapi"},
            })
        return _unique_instruments(out)

    def _index_instruments(self) -> list[dict]:
        snapshots = self._major_index_snapshots(list(_CORE_INDEXES))
        seen = {item["symbol"]: item for item in snapshots}
        out = []
        for symbol, name in _CORE_INDEXES.items():
            snap = seen.get(symbol) or {}
            exchange = symbol.split(".")[-1]
            out.append({
                "symbol": symbol,
                "name": snap.get("name") or name,
                "code": symbol.split(".")[0],
                "exchange": exchange,
                "region": "CN",
                "type": "index",
                "asset_type": "index",
                "ext": {
                    "source": "tdxapi",
                    "last_price": snap.get("last_price"),
                    "change_pct": snap.get("change_pct"),
                },
            })
        return out

    def _major_index_snapshots(self, symbols: list[str]) -> list[dict]:
        name_map = {symbol: _CORE_INDEXES.get(symbol, symbol) for symbol in symbols}
        rows = []
        for chunk in chunked(symbols, _QUOTE_BATCH):
            try:
                data = self._request(
                    "POST",
                    "/api/batch-quote",
                    json={"codes": [_to_tdx_code(symbol) for symbol in chunk]},
                )
            except Exception as e:
                logger.debug("tdx-api 核心指数快照拉取失败(%s): %s", ",".join(chunk), e)
                continue
            for item in data or []:
                row = self._quote_row(item, name_map)
                if not row:
                    continue
                rows.append({
                    "symbol": row["symbol"],
                    "name": row.get("name") or name_map.get(row["symbol"]) or row["symbol"],
                    "last_price": row.get("last_price"),
                    "change_pct": row.get("change_pct"),
                    "trend_15m": None,
                })
        return rows

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
        if dataset == "financial":
            df = self.get_financials("metrics", symbols[:1], latest_only=True)
            return _preview(self.name, "financial", df)
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
        row.update(_level_fields(item, last_price=last_price, prev_close=prev_close))
        row.update(_activity_fields(item))
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

    @staticmethod
    def _trade_history_tick_row(symbol: str, item: dict, seq_in_day: int) -> dict | None:
        dt = _parse_tdx_time(item.get("time") if item.get("time") is not None else item.get("Time"))
        if dt is None:
            return None
        if item.get("price") is not None:
            price = _float(item.get("price"))
        else:
            price = _price(item.get("Price"))
        volume = int(_float(item.get("volume") if item.get("volume") is not None else item.get("Volume")))
        raw_status = _int_or_none(item.get("status") if item.get("status") is not None else item.get("Status"))
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
            "order_count": _int_or_none(item.get("number") if item.get("number") is not None else item.get("Number")),
            "raw_status": raw_status,
            "source": "tdxapi_trade_history_minute_precision",
        }


def _level_fields(item: dict, *, last_price: float | None, prev_close: float | None) -> dict:
    """从 TDX 五档快照取盘口价格、数量和轻量衍生指标。

    集合竞价期间 TDX 会把虚拟参考价放在买一/卖一同价位置, 所以这里
    只做原始字段透传, 业务语义由 _auction_fields 单独识别。
    """
    out: dict[str, float | int | None] = {}
    bid_depth_vol = 0
    ask_depth_vol = 0
    bid_depth_amount = 0.0
    ask_depth_amount = 0.0
    for idx in range(1, 6):
        buy = _level(item.get("BuyLevel"), idx - 1)
        sell = _level(item.get("SellLevel"), idx - 1)
        bid_price = _price_or_none(buy.get("Price"))
        ask_price = _price_or_none(sell.get("Price"))
        bid_vol = _int_or_none(buy.get("Number"))
        ask_vol = _int_or_none(sell.get("Number"))
        out[f"bid{idx}_price"] = bid_price
        out[f"ask{idx}_price"] = ask_price
        out[f"bid{idx}_vol"] = bid_vol
        out[f"ask{idx}_vol"] = ask_vol
        if bid_price and bid_vol:
            bid_depth_vol += bid_vol
            bid_depth_amount += bid_price * bid_vol * 100.0
        if ask_price and ask_vol:
            ask_depth_vol += ask_vol
            ask_depth_amount += ask_price * ask_vol * 100.0

    bid1 = out.get("bid1_price")
    ask1 = out.get("ask1_price")
    spread = (ask1 - bid1) if isinstance(bid1, float) and isinstance(ask1, float) else None
    spread_pct = spread / last_price if spread is not None and last_price else None
    depth_total = bid_depth_amount + ask_depth_amount
    depth_imbalance = (
        (bid_depth_amount - ask_depth_amount) / depth_total
        if depth_total > 0
        else None
    )
    best_bid_amount = (bid1 * out["bid1_vol"] * 100.0) if bid1 and out.get("bid1_vol") else None
    best_ask_amount = (ask1 * out["ask1_vol"] * 100.0) if ask1 and out.get("ask1_vol") else None
    limit_seal_amount = None
    # 这里只记录 TDX 快照看到的一档封单事实, 不推断真实排队量。
    if (
        last_price
        and bid1
        and out.get("bid1_vol")
        and prev_close
        and abs(last_price - bid1) / max(last_price, 1e-9) <= 0.001
        and last_price >= prev_close * 1.095
    ):
        limit_seal_amount = best_bid_amount

    out.update({
        "bid1": bid1,
        "ask1": ask1,
        "spread": spread,
        "spread_pct": spread_pct,
        "bid_depth_vol": bid_depth_vol or None,
        "ask_depth_vol": ask_depth_vol or None,
        "bid_depth_amount": bid_depth_amount or None,
        "ask_depth_amount": ask_depth_amount or None,
        "depth_imbalance": depth_imbalance,
        "best_bid_amount": best_bid_amount,
        "best_ask_amount": best_ask_amount,
        "limit_seal_amount": limit_seal_amount,
    })
    return out


def _activity_fields(item: dict) -> dict:
    current_volume = _int_or_none(item.get("Intuition"))
    inside_volume = _int_or_none(item.get("InsideDish"))
    outside_volume = _int_or_none(item.get("OuterDisc"))
    outside_inside_ratio = (
        outside_volume / inside_volume
        if outside_volume is not None and inside_volume and inside_volume > 0
        else None
    )
    active_net_volume = (
        outside_volume - inside_volume
        if outside_volume is not None and inside_volume is not None
        else None
    )
    return {
        "current_volume": current_volume,
        "inside_volume": inside_volume,
        "outside_volume": outside_volume,
        "outside_inside_ratio": outside_inside_ratio,
        "active_net_volume": active_net_volume,
        "speed_rate": _float_or_none(item.get("Rate")),
        "active1": _float_or_none(item.get("Active1")),
        "active2": _float_or_none(item.get("Active2")),
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
    total_virtual = (buy_vol or 0) + unmatched_volume
    unmatched_ratio = unmatched_volume / total_virtual if total_virtual > 0 else None
    pressure_score = None
    if unmatched_ratio is not None:
        pressure_score = unmatched_ratio if unmatched_side == "buy" else -unmatched_ratio if unmatched_side == "sell" else 0.0
    return {
        "auction_price": buy_price,
        "auction_matched_volume": buy_vol,
        "auction_unmatched_side": unmatched_side,
        "auction_unmatched_volume": unmatched_volume,
        "auction_change_pct": change_pct,
        "auction_unmatched_ratio": unmatched_ratio,
        "auction_pressure_score": pressure_score,
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


def _financial_row(table: str, symbol: str, item: dict) -> dict | None:
    if not isinstance(item, dict) or not item:
        return None

    report_date = _yyyymmdd_date(item.get("UpdatedDate"))
    row: dict = {
        "symbol": symbol,
        "source": "tdxapi",
        "table": table,
        # 前端/AI 以 period_end/announce_date 为主; TDX 只有 UpdatedDate。
        "period_end": report_date.isoformat() if report_date else None,
        "announce_date": report_date.isoformat() if report_date else None,
        "report_date": report_date,
        "updated_date": report_date,
        "ipo_date": _yyyymmdd_date(item.get("IPODate")),
    }
    for out_name, raw_name in _FINANCIAL_TABLE_FIELDS[table].items():
        value = item.get(raw_name)
        if out_name in {"market", "province_code", "industry_code"}:
            row[out_name] = _int_or_none(value)
        elif out_name == "code":
            row[out_name] = str(value or "").strip()
        else:
            row[out_name] = _float_or_none(value)

    # 从 TDX 快照派生前端契约字段 / 常用比率。
    # TDX 无完整科目树, 只能覆盖能直接映射或用股本/资产推算的指标;
    # 费用明细、YoY、投资/筹资现金流等仍为空。
    revenue = _float_or_none(item.get("ZhuYingShouRu"))
    main_profit = _float_or_none(item.get("ZhuYingLiRun"))
    operating_profit = _float_or_none(item.get("YingYeLiRun"))
    total_profit = _float_or_none(item.get("LiRunZongHe"))
    net_profit = _float_or_none(item.get("JingLiRun"))
    profit_after_tax = _float_or_none(item.get("ShuiHouLiRun"))
    retained_profit = _float_or_none(item.get("WeiFenLiRun"))
    total_assets = _float_or_none(item.get("ZongZiChan"))
    net_assets = _float_or_none(item.get("JingZiChan"))
    current_assets = _float_or_none(item.get("LiuDongZiChan"))
    current_liabilities = _float_or_none(item.get("LiuDongFuZhai"))
    long_term_liabilities = _float_or_none(item.get("ChangQiFuZhai"))
    total_shares = _float_or_none(item.get("ZongGuBen"))
    op_cf = _float_or_none(item.get("JingYingXianJinLiu"))
    total_cf = _float_or_none(item.get("ZongXianJinLiu"))

    total_liabilities = None
    if current_liabilities is not None or long_term_liabilities is not None:
        total_liabilities = (current_liabilities or 0.0) + (long_term_liabilities or 0.0)

    def _safe_div(num: float | None, den: float | None) -> float | None:
        if num is None or den is None or den == 0:
            return None
        return num / den

    def _pct(num: float | None, den: float | None) -> float | None:
        ratio = _safe_div(num, den)
        return None if ratio is None else ratio * 100.0

    if table == "metrics":
        row.update({
            # 前端核心指标契约
            "eps_basic": _safe_div(net_profit, total_shares),
            "eps_diluted": _safe_div(net_profit, total_shares),
            "bps": _safe_div(net_assets, total_shares),
            "ocfps": _safe_div(op_cf, total_shares),
            "roe": _pct(net_profit, net_assets),
            "roe_diluted": _pct(net_profit, net_assets),
            "roa": _pct(net_profit, total_assets),
            # 主营利润/主营收入 近似毛利率(TDX 无完整营业成本)
            "gross_margin": _pct(main_profit, revenue),
            "net_margin": _pct(net_profit, revenue),
            "debt_to_asset_ratio": _pct(total_liabilities, total_assets),
            "operating_cash_to_revenue": _pct(op_cf, revenue),
            # 兼容旧字段
            "revenue": revenue,
            "net_income": net_profit,
            "net_operating_cash_flow": op_cf,
        })
    elif table == "income":
        row.update({
            "revenue": revenue,
            "operating_profit": operating_profit,
            "total_profit": total_profit,
            "income_tax": (
                None if total_profit is None or profit_after_tax is None
                else total_profit - profit_after_tax
            ),
            "net_income": net_profit,
            "net_income_attributable": net_profit,
            "basic_eps": _safe_div(net_profit, total_shares),
            "diluted_eps": _safe_div(net_profit, total_shares),
            "retained_earnings": retained_profit,
            # TDX 无: operating_cost / selling_expense / admin_expense / rd_expense ...
        })
    elif table == "balance_sheet":
        row.update({
            "total_assets": total_assets,
            "total_current_assets": current_assets,
            "total_non_current_assets": (
                None if total_assets is None or current_assets is None
                else total_assets - current_assets
            ),
            "accounts_receivable": _float_or_none(item.get("YingShouZhangKuan")),
            "inventory": _float_or_none(item.get("CunHuo")),
            "fixed_assets": _float_or_none(item.get("GuDingZiChan")),
            "intangible_assets": _float_or_none(item.get("WuXingZiChan")),
            "total_liabilities": total_liabilities,
            "total_current_liabilities": current_liabilities,
            "total_non_current_liabilities": long_term_liabilities,
            "total_equity": net_assets,
            "equity_attributable": net_assets,
            "retained_earnings": retained_profit,
            # 兼容旧字段
            "current_assets": current_assets,
            "current_liabilities": current_liabilities,
            "net_assets": net_assets,
        })
    elif table == "cash_flow":
        row.update({
            "net_operating_cash_flow": op_cf,
            "net_cash_change": total_cf,
            # 兼容旧字段
            "operating_cash_flow": op_cf,
            "total_cash_flow": total_cf,
            # TDX 无: investing / financing / capex
        })
    return row


def _base_url() -> str:
    return (os.getenv("TDX_API_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _symbols_from_env() -> list[str]:
    raw = os.getenv("TDX_API_REALTIME_SYMBOLS", "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _missing_quote_code(error: Exception) -> str | None:
    match = re.search(r"未查询到代码\[([^\]]+)\]", str(error))
    return match.group(1).strip() if match else None


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


def _history_date_arg(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    normalized = text.replace("-", "")
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


def _unique_instruments(rows: list[dict]) -> list[dict]:
    keyed: dict[str, dict] = {}
    for row in rows:
        symbol = row.get("symbol")
        if symbol:
            keyed[str(symbol)] = row
    return [keyed[symbol] for symbol in sorted(keyed)]


def _market_exchange_stats(value: dict) -> dict:
    up = int(_float(value.get("up")))
    down = int(_float(value.get("down")))
    flat = int(_float(value.get("flat")))
    total = int(_float(value.get("total"))) or up + down + flat
    return {"total": total, "up": up, "down": down, "flat": flat}


def _market_temperature(up_count: int, down_count: int, total_count: int) -> str:
    if total_count <= 0:
        return "unknown"
    ratio = up_count / max(down_count, 1)
    up_share = up_count / total_count
    if ratio >= 2.0 and up_share >= 0.55:
        return "hot"
    if ratio >= 1.2:
        return "warm"
    if ratio <= 0.5:
        return "cold"
    if ratio <= 0.85:
        return "cool"
    return "neutral"


def _parse_update_time_ms(value) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_CN_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return None


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


def _yyyymmdd_date(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        return None
    try:
        return date.fromisoformat(f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}")
    except ValueError:
        return None


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


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return f
    except (TypeError, ValueError):
        return None


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

    # TDX ServerTime 常用 HHMMSSmmm, 例如 143523199 表示 14:35:23.199。
    if len(text) in {8, 9, 10} and text[:6].isdigit():
        hour = int(text[:2])
        minute = int(text[2:4])
        whole_second = int(text[4:6])
        fraction = text[6:]
        if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= whole_second < 60:
            seconds = whole_second + int(fraction) / (10 ** len(fraction)) if fraction else whole_second
            midnight = now.astimezone(_CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            decoded = midnight + timedelta(hours=hour, minutes=minute, seconds=seconds)
            return int(decoded.timestamp() * 1000)

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
