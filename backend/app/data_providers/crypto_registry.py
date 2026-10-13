"""crypto 资产类别的 provider 注册表。

为什么单列一张表而不用 custom_sources 插件注册表: 插件/自定义源的 datasets 声明
是数据集级、全局的 (docs/plugin-development.md「能力声明的粒度」), 永续合约源只能
服务 crypto 资产, 注册进去会让用户能把 A 股日K/实时路由到它 (fail-open)。
本表按**资产类别**解析 provider, 与 A 股数据集路由完全隔离: A 股那条链路的
结果一个字节都不会变。

选源约束 (2026-10 生产实测, 勿凭印象换源):
  - 同一资产类别只能有一个日归属口径 (见 app/market_rules)。OKX 官方日线以
    UTC+8 零点切日, 与项目 CN_TZ 契约一致; 币安/Gate 的日线以 UTC 零点切日
    (北京 08:00), 与本类别口径冲突 —— 接入它们必须先把切日口径统一, 否则
    同一张 kline_crypto_daily 里会混两种日期口径 (静默错误)。
  - 生产机网络实测: 币安 fapi 451 / 备用域名被中间设备截断 / 经 SOCKS5 代理
    出口仍判限制地区; OKX、Gate 直连可达。故默认 OKX。

新增 crypto 源: 在此登记 name → 工厂, 并在 CRYPTO_PROVIDER_NAMES 里放行偏好值。
"""
from __future__ import annotations

from collections.abc import Callable

from app.data_providers.okx.provider import OkxProvider

_FACTORIES: dict[str, Callable[[], object]] = {
    OkxProvider.name: OkxProvider,
}

DEFAULT_CRYPTO_PROVIDER = OkxProvider.name


def names() -> set[str]:
    return set(_FACTORIES)


def has_provider(name: str | None) -> bool:
    return str(name or "").strip().lower() in _FACTORIES


def get_crypto_provider(name: str | None = None):
    """按名字取 crypto provider, 缺省用默认源。未知名字 fail-closed 抛错。"""
    key = str(name or DEFAULT_CRYPTO_PROVIDER).strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        raise ValueError(f"Unsupported crypto data provider: {name}")
    return factory()
