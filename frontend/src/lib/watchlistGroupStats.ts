/**
 * 自选分组涨跌幅 — 等权平均口径。
 *
 * 组内每只成员取「实时优先、收盘兜底」的涨跌幅(与自选表格展示同源:
 * rt_pct ?? change_pct, 百分数单位), 算术平均即分组涨跌幅。等权最贴合
 * 自选的"个人组合"视角 — 透明且无需市值数据。
 */

export interface GroupPctInfo {
  /** 等权平均涨跌幅(小数, 0.0123 = +1.23%, 与 enriched change_pct 同单位); 无有效样本为 null */
  pct: number | null
  up: number
  down: number
  flat: number
  /** 参与统计的样本数(涨跌幅非空的成员) */
  sampled: number
}

/** key: 'all' | 'ungrouped' | 分组 id */
export type GroupPctMap = Record<string, GroupPctInfo>

export function computeGroupPcts(
  entries: { symbol: string; group_id?: string | null }[],
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
    const pct = row ? row.rt_pct ?? row.change_pct : null
    add('all', pct)
    add(entry.group_id ?? 'ungrouped', pct)
  }
  const out: GroupPctMap = {}
  for (const [key, b] of buckets) {
    out[key] = {
      pct: b.pcts.length ? b.pcts.reduce((a, c) => a + c, 0) / b.pcts.length : null,
      up: b.up,
      down: b.down,
      flat: b.flat,
      sampled: b.pcts.length,
    }
  }
  return out
}

/** 涨跌幅文本: +1.23% / -0.50% / 0.00% / — (无样本)。入参为小数(0.0123) */
export function formatGroupPct(pct: number | null): string {
  if (pct == null) return '—'
  const v = pct * 100
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

/** 涨跌色 (A 股惯例红涨绿跌) */
export function groupPctColor(pct: number | null): string {
  if (pct == null || pct === 0) return 'text-muted'
  return pct > 0 ? 'text-bull' : 'text-bear'
}

/** 悬停明细: 等权平均 +1.23% · 上涨12 下跌5 平1 */
export function groupPctTitle(info: GroupPctInfo | undefined): string {
  if (!info || info.pct == null) return '暂无涨跌幅数据'
  return `等权平均 ${formatGroupPct(info.pct)} · 上涨${info.up} 下跌${info.down} 平${info.flat} (共${info.sampled}只)`
}
