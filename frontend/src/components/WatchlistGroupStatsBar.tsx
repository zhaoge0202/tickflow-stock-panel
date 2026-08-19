import type { WatchlistGroup } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import {
  groupPctColor,
  groupMetricTitle,
  groupMetricValue,
  GROUP_METRICS,
  sortGroupKeys,
  type GroupPctMap,
  type GroupStatsConfig,
  type GroupStatsConfigPatch,
} from '@/lib/watchlistGroupStats'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'
import { GroupStatsSettings } from '@/components/GroupStatsSettings'
import type { WatchlistGroupFilter } from '@/components/WatchlistGroups'

/**
 * 自选「分组统计条」— 页面顶部的图形化分组涨跌概览。
 *
 * 每个分组一行: 中轴分叉条形图直观对比各组强弱 (红=强 向右, 绿=弱 向左,
 * 长度按各组最大绝对值归一化)。指标与排序可配置 (与分组卡片视图共享,
 * 由自选页统一持有并持久化):
 * - 指标: 等权平均 / 中位数 / 上涨占比(50%强弱轴) / 组内最强 / 组内最弱
 * - 排序: 分组定义顺序 / 按指标降序 / 升序
 * 数据来自自选页既有的分组涨跌统计 (groupPcts), 零额外请求; 点击行钻取分组列表。
 */

interface Row {
  key: string
  name: string
  dot: string
  text: string
  count: number
  selected: boolean
}

export function WatchlistGroupStatsBar({
  groups,
  counts,
  pcts,
  selected,
  onSelect,
  config,
  onConfigChange,
}: {
  groups: WatchlistGroup[]
  counts: Record<string, number>
  pcts: GroupPctMap
  selected?: WatchlistGroupFilter
  onSelect: (group: WatchlistGroupFilter) => void
  config: GroupStatsConfig
  onConfigChange: (patch: GroupStatsConfigPatch) => void
}) {
  const rows: Row[] = groups.map(group => {
    const color = resolveWatchlistGroupColor(group.color)
    return {
      key: group.id,
      name: group.name,
      dot: color.dot,
      text: color.text,
      count: counts[group.id] ?? 0,
      selected: selected === group.id,
    }
  })
  if ((counts.ungrouped ?? 0) > 0) {
    rows.push({
      key: 'ungrouped',
      name: '未分组',
      dot: 'bg-muted/60',
      text: 'text-foreground',
      count: counts.ungrouped ?? 0,
      selected: selected === 'ungrouped',
    })
  }

  const metricLabel = GROUP_METRICS.find(m => m.id === config.metric)?.label ?? ''
  const valueOf = (key: string) => groupMetricValue(pcts[key], config.metric)

  if (rows.length === 0) return null

  const ordered = sortGroupKeys(rows, r => r.key, pcts, config)

  // 条长归一化基准: 上涨占比以 0.5 强弱轴的偏离量为幅值, 其余指标取绝对值
  const magnitude = (v: number) => config.metric === 'up_ratio' ? Math.abs(v - 0.5) : Math.abs(v)
  const maxMag = Math.max(...ordered.reduce<number[]>((acc, r) => {
    const v = valueOf(r.key)
    if (v != null) acc.push(magnitude(v))
    return acc
  }, [0.0001]))

  return (
    <div className="border-b border-border bg-surface/40 px-5 py-2">
      <div className="mb-1 flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-wider text-muted">分组涨跌 · {metricLabel}</div>
        <GroupStatsSettings config={config} onChange={onConfigChange} />
      </div>
      <div className="flex flex-col gap-px">
        {ordered.map(r => {
          const info = pcts[r.key]
          const v = valueOf(r.key)
          // 条形方向: 上涨占比以 0.5 为轴, 其余以 0 为轴; 幅值按组间最大值归一化
          const signed = config.metric === 'up_ratio' ? (v == null ? null : v - 0.5) : v
          const half = v == null ? 0 : Math.min(50, (magnitude(v) / maxMag) * 50)
          const isUp = signed != null && signed > 0
          const isDown = signed != null && signed < 0
          const label = v == null
            ? '—'
            : config.metric === 'up_ratio'
              ? `${(v * 100).toFixed(0)}%`
              : fmtPct(v)
          return (
            <button
              key={r.key}
              type="button"
              onClick={() => onSelect(r.key)}
              title={groupMetricTitle(info, config.metric)}
              className={`grid grid-cols-[minmax(72px,auto)_1fr_auto_auto] items-center gap-2.5 rounded px-1.5 py-1 text-left transition-colors ${
                r.selected ? 'bg-accent/10' : 'hover:bg-elevated/50'
              }`}
            >
              <span className="flex min-w-0 items-center gap-1.5">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${r.dot}`} />
                <span className={`truncate text-xs ${r.selected ? 'text-foreground' : r.text}`}>{r.name}</span>
                <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted">{r.count}</span>
              </span>
              <span className="relative h-3.5 rounded bg-elevated/40">
                <span className="absolute inset-y-0 left-1/2 w-px bg-border" />
                {isUp && (
                  <span
                    className="absolute inset-y-[3px] left-1/2 rounded-r bg-bull/75 transition-[width] duration-300"
                    style={{ width: `${half}%` }}
                  />
                )}
                {isDown && (
                  <span
                    className="absolute inset-y-[3px] right-1/2 rounded-l bg-bear/75 transition-[width] duration-300"
                    style={{ width: `${half}%` }}
                  />
                )}
              </span>
              <span className={`w-16 text-right font-mono text-xs font-semibold tabular-nums ${groupPctColor(signed)}`}>
                {label}
              </span>
              <span className="w-[72px] shrink-0 text-right text-[10px] tabular-nums">
                {info && info.sampled > 0 ? (
                  <>
                    <span className="text-bull">{info.up}涨</span>
                    <span className="mx-0.5 text-muted/40">/</span>
                    <span className="text-bear">{info.down}跌</span>
                  </>
                ) : (
                  <span className="text-muted">—</span>
                )}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
