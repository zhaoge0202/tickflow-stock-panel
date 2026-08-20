/**
 * 自选分组涨跌幅 — 等权平均口径。
 *
 * 组内每只成员取「实时优先、收盘兜底」的涨跌幅(与自选表格展示同源:
 * rt_pct ?? change_pct, 小数单位), 算术平均即分组涨跌幅。等权最贴合
 * 自选的"个人组合"视角 — 透明且无需市值数据。
 */

import { fmtPct } from '@/lib/format'
import { storage } from '@/lib/storage'

export interface GroupPctInfo {
  /** 等权平均涨跌幅(小数, 0.0123 = +1.23%, 与 enriched change_pct 同单位); 无有效样本为 null */
  pct: number | null
  up: number
  down: number
  flat: number
  /** 参与统计的样本数(涨跌幅非空的成员) */
  sampled: number
  /** 中位数涨跌幅(小数); 无有效样本为 null */
  median: number | null
  /** 组内最大涨跌幅(小数); 无有效样本为 null */
  max: number | null
  /** 组内最小涨跌幅(小数); 无有效样本为 null */
  min: number | null
}

/** key: 'all' | 'ungrouped' | 分组 id */
export type GroupPctMap = Record<string, GroupPctInfo>

/**
 * 单只标的的展示涨跌幅: 实时优先、收盘兜底(与自选表格/卡片展示同源,
 * 小数单位), 无有效数据为 null。分组统计与分组卡片排序共用此口径。
 */
export function rowPct(
  row: { rt_pct?: number | null; change_pct?: number | null } | undefined,
): number | null {
  const pct = row ? row.rt_pct ?? row.change_pct : null
  return pct == null || !Number.isFinite(pct) ? null : pct
}

export function computeGroupPcts(
  entries: { symbol: string; group_ids?: string[] | null }[],
  rowsBySymbol: Map<string, { rt_pct?: number | null; change_pct?: number | null }>,
): GroupPctMap {
  const buckets = new Map<string, { pcts: number[]; up: number; down: number; flat: number }>()
  const add = (key: string, pct: number | null | undefined) => {
    if (pct == null || !Number.isFinite(pct)) return
    const b = buckets.get(key) ?? { pcts: [], up: 0, down: 0, flat: 0 }
    b.pcts.push(pct)
    if (pct > 0) b.up++
    else if (pct < 0) b.down++
    else b.flat++
    buckets.set(key, b)
  }
  for (const entry of entries) {
    const row = rowsBySymbol.get(entry.symbol)
    const pct = rowPct(row)
    add('all', pct)
    // 多组并存: 一股计入每个所属分组; 不属于任何分组才计未分组
    const gids = entry.group_ids ?? []
    if (gids.length === 0) add('ungrouped', pct)
    else for (const gid of gids) add(gid, pct)
  }
  const out: GroupPctMap = {}
  for (const [key, b] of buckets) {
    const sortedPcts = [...b.pcts].sort((a, c) => a - c)
    const mid = Math.floor(sortedPcts.length / 2)
    const median = sortedPcts.length === 0
      ? null
      : sortedPcts.length % 2 === 1
        ? sortedPcts[mid]
        : (sortedPcts[mid - 1] + sortedPcts[mid]) / 2
    out[key] = {
      pct: b.pcts.length ? b.pcts.reduce((a, c) => a + c, 0) / b.pcts.length : null,
      up: b.up,
      down: b.down,
      flat: b.flat,
      sampled: b.pcts.length,
      median,
      max: sortedPcts.length ? sortedPcts[sortedPcts.length - 1] : null,
      min: sortedPcts.length ? sortedPcts[0] : null,
    }
  }
  return out
}

/** 涨跌色 (A 股惯例红涨绿跌) */
export function groupPctColor(pct: number | null): string {
  if (pct == null || pct === 0) return 'text-muted'
  return pct > 0 ? 'text-bull' : 'text-bear'
}

// ===== 分组统计条指标契约 =====

/** 分组统计指标: 等权平均 / 中位数 / 上涨占比(以50%为轴) / 组内最强 / 组内最弱 */
export type GroupMetric = 'mean' | 'median' | 'up_ratio' | 'max' | 'min'

export const GROUP_METRICS: ReadonlyArray<{ id: GroupMetric; label: string; hint: string }> = [
  { id: 'mean', label: '等权平均', hint: '组内涨跌幅算术平均' },
  { id: 'median', label: '中位数', hint: '组内涨跌幅中位值, 抗极值' },
  { id: 'up_ratio', label: '上涨占比', hint: '上涨家数占有效样本比例, 50% 为强弱轴' },
  { id: 'max', label: '组内最强', hint: '组内最大涨幅 (龙头强度)' },
  { id: 'min', label: '组内最弱', hint: '组内最小涨幅' },
]

export function isGroupMetric(v: unknown): v is GroupMetric {
  return typeof v === 'string' && GROUP_METRICS.some(m => m.id === v)
}

/** 分组排序方式: 定义顺序 / 按指标降序 / 升序 (分组统计条与分组卡片共享) */
export type GroupSort = 'default' | 'desc' | 'asc'

export const GROUP_SORT_OPTIONS: ReadonlyArray<{ id: GroupSort; label: string }> = [
  { id: 'default', label: '定义顺序' },
  { id: 'desc', label: '降序' },
  { id: 'asc', label: '升序' },
]

export function isGroupSort(v: unknown): v is GroupSort {
  return v === 'default' || v === 'desc' || v === 'asc'
}

/** 分组指标+排序配置 (分组统计条 / 分组卡片两个视图共享同一份持久化) */
export interface GroupStatsConfig {
  metric: GroupMetric
  sort: GroupSort
  /** 分组卡片默认展示的成员条数 (前 N, 可展开全部) */
  cardTopN: number
  /** 分组卡片头部是否显示分组颜色底条 */
  cardColorBar: boolean
  /** 分组卡片成员行是否显示序号 */
  cardRank: boolean
}

/** 卡片默认条数与上下限 (超出范围的持久化值会被夹回) */
export const GROUP_CARD_TOP_N_DEFAULT = 8
export const GROUP_CARD_TOP_N_MIN = 1
export const GROUP_CARD_TOP_N_MAX = 50
/** 卡片头部彩条 / 成员行序号默认开启 (旧持久化缺失该字段时回退到默认) */
export const GROUP_CARD_COLOR_BAR_DEFAULT = true
export const GROUP_CARD_RANK_DEFAULT = true

function normalizeBool(v: unknown, fallback: boolean): boolean {
  return typeof v === 'boolean' ? v : fallback
}

export function normalizeGroupCardTopN(v: unknown): number {
  const n = typeof v === 'number' ? Math.round(v) : Number.NaN
  if (!Number.isFinite(n)) return GROUP_CARD_TOP_N_DEFAULT
  return Math.min(GROUP_CARD_TOP_N_MAX, Math.max(GROUP_CARD_TOP_N_MIN, n))
}

export function loadGroupStatsConfig(): GroupStatsConfig {
  const saved = storage.watchlistGroupStats.get({
    metric: 'mean',
    sort: 'default',
    cardTopN: GROUP_CARD_TOP_N_DEFAULT,
    cardColorBar: GROUP_CARD_COLOR_BAR_DEFAULT,
    cardRank: GROUP_CARD_RANK_DEFAULT,
  })
  return {
    metric: isGroupMetric(saved.metric) ? saved.metric : 'mean',
    sort: isGroupSort(saved.sort) ? saved.sort : 'default',
    cardTopN: normalizeGroupCardTopN(saved.cardTopN),
    cardColorBar: normalizeBool(saved.cardColorBar, GROUP_CARD_COLOR_BAR_DEFAULT),
    cardRank: normalizeBool(saved.cardRank, GROUP_CARD_RANK_DEFAULT),
  }
}

/** 配置局部更新 (设置弹层 -> 持有方), 新增卡片显示项时在此处扩展 */
export type GroupStatsConfigPatch = Partial<Pick<GroupStatsConfig, 'metric' | 'sort' | 'cardTopN' | 'cardColorBar' | 'cardRank'>>

/** 按配置排序分组键列表 (null 排最后), sort='default' 时原序返回 */
export function sortGroupKeys<T>(items: T[], keyOf: (item: T) => string, pcts: GroupPctMap, config: GroupStatsConfig): T[] {
  if (config.sort === 'default') return items
  return [...items].sort((a, b) => {
    const va = groupMetricValue(pcts[keyOf(a)], config.metric)
    const vb = groupMetricValue(pcts[keyOf(b)], config.metric)
    if (va == null && vb == null) return 0
    if (va == null) return 1
    if (vb == null) return -1
    return config.sort === 'desc' ? vb - va : va - vb
  })
}

/**
 * 取分组在指定指标下的数值。
 * 涨跌幅类指标返回小数 (0.0123 = +1.23%); 上涨占比返回 0~1 占比,
 * 条形渲染时以 0.5 为强弱轴。无有效样本为 null。
 */
export function groupMetricValue(info: GroupPctInfo | undefined, metric: GroupMetric): number | null {
  if (!info || info.sampled === 0) return null
  switch (metric) {
    case 'mean': return info.pct
    case 'median': return info.median
    case 'up_ratio': return info.up / info.sampled
    case 'max': return info.max
    case 'min': return info.min
  }
}

/** 悬停明细: 按当前指标给出数值 + 全套统计, 任意指标下信息完整 */
export function groupMetricTitle(info: GroupPctInfo | undefined, metric: GroupMetric): string {
  if (!info || info.sampled === 0) return '暂无涨跌幅数据'
  const value = groupMetricValue(info, metric)
  const metricLabel = GROUP_METRICS.find(m => m.id === metric)?.label ?? ''
  const valueText = value == null
    ? '—'
    : metric === 'up_ratio'
      ? `${(value * 100).toFixed(1)}%`
      : fmtPct(value)
  return `${metricLabel} ${valueText} · 等权 ${fmtPct(info.pct)} · 中位 ${fmtPct(info.median)} · 最强 ${fmtPct(info.max)} · 最弱 ${fmtPct(info.min)} · 上涨${info.up} 下跌${info.down} 平${info.flat} (共${info.sampled}只)`
}

/** 悬停明细: 等权平均 +1.23% · 上涨12 下跌5 平1 (格式化复用全站 fmtPct) */
export function groupPctTitle(info: GroupPctInfo | undefined): string {
  if (!info || info.pct == null) return '暂无涨跌幅数据'
  return `等权平均 ${fmtPct(info.pct)} · 上涨${info.up} 下跌${info.down} 平${info.flat} (共${info.sampled}只)`
}
