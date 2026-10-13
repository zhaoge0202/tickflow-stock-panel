"""crypto 资产类别的 provider 注册表。

为什么单列一张表而不用 custom_sources 插件注册表: 插件/自定义源的 datasets 声明
是数据集级、全局的 (docs/plugin-development.md「能力声明的粒度」), 币安永续只能
服务 crypto 资产, 注册进去会让用户能把 A 股日K/实时路由到它 (fail-open)。
本表按**资产类别**解析 provider, 与 A 股数据集路由完全隔离: A 股那条链路的
结果一个字节都不会变。

新增 crypto 源: 在此登记 name → 工厂, 并在 CRYPTO_PROVIDER_NAMES 里放行偏好值。
"""
from __future__ import annotations

from collections.abc import Callable

from app.data_providers.binance.provider import BinanceProvider

_FACTORIES: dict[str, Callable[[], object]] = {
    BinanceProvider.name: BinanceProvider,
}

DEFAULT_CRYPTO_PROVIDER = BinanceProvider.name


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
