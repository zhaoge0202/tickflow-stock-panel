"""扶摇(同花顺金融数据 API)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

当前实现数据集: realtime (A 股全市场快照, 分页)。
未声明 daily / minute / financial → provider_has_dataset 为 False, 自动回退 tickflow。

单位口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - 扶摇 price_change_ratio_pct 为百分数数值 (1.74 = +1.74%), 本项目 realtime
    change_pct 契约为小数制 (0.0174 = 1.74%) → 此处显式 / 100。
  - volume 单位股、turnover 单位元, 与内部契约一致, 直接透传。
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field

from app.plugins.fuyao import client as fuyao_client
from app.plugins.fuyao.client import FuyaoClient, FuyaoError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("realtime",)

API_KEY_ENV = "FUYAO_API_KEY"
SECRETS_FIELD = "fuyao_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env


def get_api_key() -> str:
    from app import secrets_store
    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: API Key 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    return False, f"未配置 {API_KEY_ENV}(可在设置页数据源卡片中直接填写)"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次快照接口(先探后存, 对齐 /tickflow-key 语义)。不落盘。"""
    client = None
    try:
        client = fuyao_client.FuyaoClient(api_key=api_key, timeout=10.0)
        client.snapshot_page(limit=1)
        return True, "ok"
    except FuyaoError as e:
        return False, f"Key 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _FuyaoConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "fuyao"
    display_name: str = "fuyao"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: dict, *names: str):
    """按优先级取第一个非 None 字段。实测字段名与官方文档示例不一致, 两者兼容。"""
    for n in names:
        if row.get(n) is not None:
            return row.get(n)
    return None


def _map_snapshot_row(row: dict, fetched_ms: int) -> dict | None:
    """扶摇快照行 → 内部 realtime record。字段缺失时按依赖推导, 不伪造数据。

    实测字段(2026-08): high_price / low_price / prev_price;
    官方文档示例: highest_price / lowest_price / prev_close_price。两者都取。
    """
    symbol = row.get("thscode")
    if not symbol:
        return None
    last = _to_float(row.get("last_price"))
    prev = _to_float(_first(row, "prev_price", "prev_close_price"))

    # 百分数 (1.74 = +1.74%) → 小数制 (0.0174), 契约见模块 docstring
    pct = _to_float(row.get("price_change_ratio_pct"))
    change_pct = pct / 100.0 if pct is not None else None

    change_amount = _to_float(row.get("price_change"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    return {
        "symbol": symbol,
        "name": row.get("name"),  # 快照无名称, 由下游维表关联
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("open_price")),
        "high": _to_float(_first(row, "high_price", "highest_price")),
        "low": _to_float(_first(row, "low_price", "lowest_price")),
        "volume": _to_float(row.get("volume")),
        "amount": _to_float(row.get("turnover")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,      # 快照未提供, 不启发式计算
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道用历史股本计算
        "timestamp": fetched_ms,
        "session": None,
    }


class FuyaoProvider:
    """扶摇数据源。realtime = A 股全市场快照(quote_service 全市场模式轮询调用)。"""

    name = "fuyao"
    builtin = True

    def __init__(self) -> None:
        self.config = _FuyaoConfig()
        self._client: FuyaoClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> FuyaoClient:
        if self._client is None:
            self._client = fuyao_client.FuyaoClient(api_key=get_api_key())
        return self._client

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            rows, server_ts = self._get_client().snapshot_all()
        except FuyaoError as e:
            logger.warning("扶摇实时行情拉取失败: %s", e)
            return []

        # 优先用服务端时间戳(行情归属); 缺失时退回本地时间
        fetched_ms = server_ts or int(time.time() * 1000)

        records = []
        dropped = 0
        for row in rows:
            rec = _map_snapshot_row(row, fetched_ms)
            if rec is not None:
                records.append(rec)
            else:
                dropped += 1
        if dropped and not records:
            # 整页都识别不出 thscode → 大概率接口 schema 变了, 明确告警而非静默空数据
            logger.warning("扶摇快照 %d 行全部缺少 thscode 字段, 疑似接口结构变化", dropped)
            return []
        logger.info("扶摇实时行情拉取完成: %d 条(丢弃 %d 行)", len(records), dropped)
        return records

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "realtime":
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "error": f"扶摇插件未接入 {dataset} 数据集(自动回退 TickFlow)"}
        try:
            rows, count = self._get_client().snapshot_page(limit=5)
        except FuyaoError as e:
            return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
        fetched_ms = int(time.time() * 1000)
        head = [r for r in (_map_snapshot_row(row, fetched_ms) for row in rows) if r][:5]
        return {
            "provider": self.name,
            "dataset": "realtime",
            "rows": count or len(head),
            "columns": list(head[0].keys()) if head else [],
            "preview": head,
        }
