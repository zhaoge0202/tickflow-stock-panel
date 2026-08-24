"""扶摇(同花顺金融数据 API) HTTP 客户端。

职责: 认证、统一信封解包、分页拉取快照。不知道 provider / services 层。
文档: https://fuyao.aicubes.cn/docs — REST + X-api-key, 响应信封 {code, message, data}。
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"

# A 股约 5400 只, 500/页约 11 页; 50 页上限防御 count 异常导致的死循环。
_SNAPSHOT_PAGE_SIZE = 500
_SNAPSHOT_MAX_PAGES = 50
_PAGE_INTERVAL_S = 0.15  # 页间隔, 降低触发限频 (code=4001) 的概率


class FuyaoError(Exception):
    """扶摇接口错误(配置缺失 / 网络失败 / 信封 code != 0)。"""


class FuyaoClient:
    """扶摇 REST 客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        if not api_key:
            raise FuyaoError("未配置 FUYAO_API_KEY")
        self.last_server_ts = 0  # 最近一页响应里的服务端时间戳(ms), 供行情归属
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-api-key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # ---- 内部 ----
    def _get(self, path: str, params: dict) -> dict:
        """GET + 信封解包。code != 0 时抛 FuyaoError(含 code 与 message)。"""
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise FuyaoError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise FuyaoError(f"HTTP {resp.status_code}: {path}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise FuyaoError(f"响应不是 JSON: {path}") from e
        code = payload.get("code")
        if code not in (0, "0", None):
            raise FuyaoError(f"扶摇接口错误 code={code}: {payload.get('message', '')} ({path})")
        return payload.get("data") or {}

    # ---- 快照 ----
    def snapshot_page(self, limit: int = _SNAPSHOT_PAGE_SIZE, offset: int = 0) -> tuple[list[dict], int]:
        """拉取一页 A 股全市场快照。返回 (rows, total), total 为全市场总数。

        实测响应(2026-08): data={timestamp, total, item}; 官方文档示例为
        data={count, data}。两者都兼容, 以实测为准。
        """
        data = self._get("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
        try:
            self.last_server_ts = int(data.get("timestamp") or 0)
        except (TypeError, ValueError):
            self.last_server_ts = 0
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = data.get("data") if isinstance(data.get("data"), list) else []
        raw_total = data.get("total")
        if raw_total is None:
            raw_total = data.get("count") or 0
        try:
            total = int(raw_total or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total

    def snapshot_all(self) -> tuple[list[dict], int]:
        """分页拉取全市场快照。返回 (rows, 服务端时间戳ms)。

        服务端时间戳用于行情归属; 缺失时返回 0, 由调用方退回本地时间。
        空数据 / 中途失败时抛 FuyaoError。
        """
        out: list[dict] = []
        server_ts = 0
        offset = 0
        for page in range(_SNAPSHOT_MAX_PAGES):
            if page > 0:
                time.sleep(_PAGE_INTERVAL_S)
            rows, total = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if not server_ts:
                server_ts = self.last_server_ts
            if total and len(out) >= total:
                break
            offset += len(rows)
        if not out:
            raise FuyaoError("全市场快照为空")
        return out, server_ts
