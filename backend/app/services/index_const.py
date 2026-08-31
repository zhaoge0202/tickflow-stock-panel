"""实时指数核心清单 — 产品级固定契约 (单一权威)。

指数展示层 (侧栏指数条 / 市场总览) 固定四只核心指数, 不开放配置:
- 数据源边界: TickFlow 与 fuyao 指数快照双源均完整覆盖, 无降级分歧;
- 后端消费方 (quote_service / overview / sector_monitor) 与前端 Layout
  统一引用此处, 不得各自维护副本。

监控规则的指数标的不受此限 — quote_service 会把启用规则的指数并入显式拉取。
"""

CORE_INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000680.SH": "科创综指",
}

CORE_INDEX_SYMBOLS: tuple[str, ...] = tuple(CORE_INDEX_NAMES.keys())
