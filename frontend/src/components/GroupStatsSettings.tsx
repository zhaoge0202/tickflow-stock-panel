import { useEffect, useRef, useState } from 'react'
import { Minus, Plus, SlidersHorizontal } from 'lucide-react'
import {
  GROUP_METRICS,
  GROUP_SORT_OPTIONS,
  GROUP_CARD_TOP_N_MAX,
  GROUP_CARD_TOP_N_MIN,
  type GroupStatsConfig,
  type GroupStatsConfigPatch,
} from '@/lib/watchlistGroupStats'

/**
 * 分组「指标 + 排序」设置弹层 — 分组统计条与分组卡片共用。
 * 状态由父级持有并持久化, 这里只负责弹层交互与展示。
 * showCardLimit 为真时额外暴露分组卡片显示项 (条数/头部彩条/序号, 仅卡片视图有意义)。
 */
export function GroupStatsSettings({
  config,
  onChange,
  ariaLabel = '分组统计设置',
  showCardLimit = false,
}: {
  config: GroupStatsConfig
  onChange: (patch: GroupStatsConfigPatch) => void
  ariaLabel?: string
  showCardLimit?: boolean
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  // 点击面板外部关闭 (与自选页搜索框同模式)
  useEffect(() => {
    if (!open) return
    const handleClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-label={ariaLabel}
        title={ariaLabel}
        className={`inline-flex h-5 w-5 items-center justify-center rounded transition-colors ${
          open
            ? 'text-accent bg-accent/10'
            : 'text-muted hover:text-foreground hover:bg-elevated'
        }`}
      >
        <SlidersHorizontal className="h-3 w-3" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-30 mt-1 w-64 rounded-card border border-border bg-base p-3 shadow-xl">
          <div className="text-[10px] uppercase tracking-wider text-muted">指标</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {GROUP_METRICS.map(m => (
              <button
                key={m.id}
                type="button"
                title={m.hint}
                onClick={() => onChange({ metric: m.id })}
                className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                  config.metric === m.id
                    ? 'bg-accent/15 text-accent'
                    : 'bg-elevated text-secondary hover:text-foreground hover:bg-elevated/80'
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>
          <div className="mt-2.5 text-[10px] uppercase tracking-wider text-muted">排序</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {GROUP_SORT_OPTIONS.map(s => (
              <button
                key={s.id}
                type="button"
                onClick={() => onChange({ sort: s.id })}
                className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                  config.sort === s.id
                    ? 'bg-accent/15 text-accent'
                    : 'bg-elevated text-secondary hover:text-foreground hover:bg-elevated/80'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
          {showCardLimit && (
            <>
              <div className="mt-2.5 text-[10px] uppercase tracking-wider text-muted">卡片显示</div>
              <div className="mt-1 flex items-center gap-2" title="分组卡片默认展示组内前 N 条, 可展开查看全部">
                <button
                  type="button"
                  aria-label="减少卡片显示条数"
                  disabled={config.cardTopN <= GROUP_CARD_TOP_N_MIN}
                  onClick={() => onChange({ cardTopN: config.cardTopN - 1 })}
                  className="inline-flex h-5 w-5 items-center justify-center rounded bg-elevated text-secondary transition-colors hover:text-foreground hover:bg-elevated/80 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Minus className="h-3 w-3" />
                </button>
                <span className="w-16 text-center font-mono text-[11px] tabular-nums text-secondary">
                  前 {config.cardTopN} 条
                </span>
                <button
                  type="button"
                  aria-label="增加卡片显示条数"
                  disabled={config.cardTopN >= GROUP_CARD_TOP_N_MAX}
                  onClick={() => onChange({ cardTopN: config.cardTopN + 1 })}
                  className="inline-flex h-5 w-5 items-center justify-center rounded bg-elevated text-secondary transition-colors hover:text-foreground hover:bg-elevated/80 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Plus className="h-3 w-3" />
                </button>
              </div>
              <div className="mt-2 space-y-1.5">
                <div className="flex items-center justify-between" title="分组卡片头部是否显示分组颜色底条">
                  <span className="text-[11px] text-secondary">头部颜色</span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={config.cardColorBar}
                    aria-label="切换卡片头部颜色"
                    onClick={() => onChange({ cardColorBar: !config.cardColorBar })}
                    className={`relative h-4 w-7 shrink-0 rounded-full transition-colors ${
                      config.cardColorBar ? 'bg-accent/60' : 'bg-elevated hover:bg-elevated/80'
                    }`}
                  >
                    <span
                      className={`absolute top-0.5 h-3 w-3 rounded-full transition-all ${
                        config.cardColorBar ? 'left-[14px] bg-white' : 'left-0.5 bg-muted'
                      }`}
                    />
                  </button>
                </div>
                <div className="flex items-center justify-between" title="成员行左侧是否显示排名序号">
                  <span className="text-[11px] text-secondary">序号</span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={config.cardRank}
                    aria-label="切换卡片序号显示"
                    onClick={() => onChange({ cardRank: !config.cardRank })}
                    className={`relative h-4 w-7 shrink-0 rounded-full transition-colors ${
                      config.cardRank ? 'bg-accent/60' : 'bg-elevated hover:bg-elevated/80'
                    }`}
                  >
                    <span
                      className={`absolute top-0.5 h-3 w-3 rounded-full transition-all ${
                        config.cardRank ? 'left-[14px] bg-white' : 'left-0.5 bg-muted'
                      }`}
                    />
                  </button>
                </div>
              </div>
            </>
          )}
          <div className="mt-2.5 text-[10px] leading-relaxed text-muted">
            {GROUP_METRICS.find(m => m.id === config.metric)?.hint}
          </div>
        </div>
      )}
    </div>
  )
}
