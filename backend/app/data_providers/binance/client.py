"""币安 fapi REST 客户端 — 只负责取数与字段拆解, 不做口径转换。

口径转换 (合约日归属、北京墙钟、单位) 在 provider 层完成, 与本文件分离:
client 返回原始 dict / list, provider 负责映射到内部契约。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://fapi.binance.com"
DEFAULT_TIMEOUT = 20.0

# klines 单次返回上限 (币安 fapi 文档 limit 最大 1500)
MAX_KLINES_LIMIT = 1500


class BinanceClientError(RuntimeError):
    """取数失败 (网络/HTTP/业务错误), 由 provider 决定软失败或上抛。"""


class BinanceClient:
    """fapi 只读客户端。无鉴权 —— 仅使用公开市场数据端点。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, params=params or {})
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            raise BinanceClientError(f"GET {path} failed: {e}") from e
        except ValueError as e:  # 非 JSON 响应
            raise BinanceClientError(f"GET {path} returned non-JSON body: {e}") from e

    def exchange_info(self) -> dict:
        """合约交易对清单 (含 status / contractType / baseAsset)。"""
        data = self._get("/fapi/v1/exchangeInfo")
        if not isinstance(data, dict):
            raise BinanceClientError("exchangeInfo 响应结构异常")
        return data

    def klines(
        self,
        symbol: str,
        interval: str = "1d",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[list]:
        """K 线原始数组: [openTime, open, high, low, close, volume, closeTime,
        quoteVolume, trades, ...]。

        时间戳单位毫秒 UTC; 数值均为字符串, 由 provider 转 float。
        """
        params: dict[str, Any] = {"symbol": symbol, "interval": interval}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        params["limit"] = int(max(1, min(limit or MAX_KLINES_LIMIT, MAX_KLINES_LIMIT)))
        data = self._get("/fapi/v1/klines", params)
        if not isinstance(data, list):
            raise BinanceClientError(f"klines 响应结构异常: {symbol}")
        return data
