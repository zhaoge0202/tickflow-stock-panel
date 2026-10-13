"""OKX v5 公开市场 REST 客户端 — 只负责取数, 不做口径转换。

为什么默认走 OKX 而不是币安: 生产机(Windows)实测币安不可用 —— fapi.binance.com 返回
451(地区限制), fapi1/fapi2 被网络中间设备以 202+空 body 截断, 经 SOCKS5 代理出口访问
仍被币安判为限制地区。OKX/Gate 从同一网络直连可达且为官方公开端点 (免费、无需 key)。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.okx.com"
DEFAULT_TIMEOUT = 20.0

# candles 单次上限 300; history-candles 单次上限 100
CANDLES_MAX_LIMIT = 300
HISTORY_CANDLES_MAX_LIMIT = 100


class OkxClientError(RuntimeError):
    """取数失败 (网络/HTTP/业务码非 0), 由 provider 决定软失败或上抛。"""


class OkxClient:
    """OKX v5 公开市场只读客户端。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> list:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, params=params or {})
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise OkxClientError(f"GET {path} failed: {e}") from e
        except ValueError as e:  # 非 JSON 响应 (被中间层截断时会走到这里)
            raise OkxClientError(f"GET {path} returned non-JSON body: {e}") from e

        if not isinstance(payload, dict):
            raise OkxClientError(f"GET {path} 响应结构异常")
        code = str(payload.get("code", ""))
        if code != "0":
            raise OkxClientError(f"GET {path} business error code={code} msg={payload.get('msg')!r}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise OkxClientError(f"GET {path} data 字段不是数组")
        return data

    def instruments(self, inst_type: str = "SWAP") -> list[dict]:
        """合约清单。OKX 不提供分页参数, 一次性返回该类型全部合约。"""
        return self._get("/api/v5/public/instruments", {"instType": inst_type})

    def candles(self, inst_id: str, bar: str = "1D", limit: int = 100) -> list[list]:
        """近端 K 线 (新→旧)。行结构: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]。"""
        return self._get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": max(1, min(int(limit), CANDLES_MAX_LIMIT))},
        )

    def history_candles(self, inst_id: str, bar: str, after_ms: int, limit: int = 100) -> list[list]:
        """历史 K 线: 返回 ts 早于 after_ms 的数据 (新→旧), 用于翻页回补。"""
        return self._get(
            "/api/v5/market/history-candles",
            {
                "instId": inst_id,
                "bar": bar,
                "after": int(after_ms),
                "limit": max(1, min(int(limit), HISTORY_CANDLES_MAX_LIMIT)),
            },
        )

    def ticker(self, inst_id: str) -> dict:
        rows = self._get("/api/v5/market/ticker", {"instId": inst_id})
        return rows[0] if rows else {}

    @staticmethod
    def sleep_for_rate_limit(seconds: float = 0.12) -> None:
        """OKX 公开行情按 IP 限速; 批量翻页时给请求之间留间隔。"""
        time.sleep(max(0.0, seconds))
