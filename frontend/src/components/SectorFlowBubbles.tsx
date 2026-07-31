import { useMemo, useEffect, useState } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/cn'

/** 板块动能气泡的最小数据契约 (概念/行业 stats 都满足) */
export interface SectorFlowItem {
  key: string
  avgPct: number | null
  totalAmount: number
  heatScore: number
  count: number
  upCount: number
  downCount: number
}

type Props = {
  items: SectorFlowItem[]
  selectedKey?: string | null
  onSelect?: (key: string) => void
  title?: string
  height?: number
  /** 最多渲染气泡数, 避免 force 布局卡顿 */
  maxItems?: number
  className?: string
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v))
}

function pctColor(pct: number | null): string {
  if (pct == null || !Number.isFinite(pct) || pct === 0) return 'rgba(148,163,184,0.85)'
  // 小数制: 0.05 = 5%
  const intensity = clamp(Math.abs(pct) / 0.08, 0.35, 1)
  const a = 0.35 + intensity * 0.55
  if (pct > 0) return `rgba(239, 68, 68, ${a.toFixed(2)})`
  return `rgba(16, 185, 129, ${a.toFixed(2)})`
}

function borderColor(pct: number | null, selected: boolean): string {
  if (selected) return 'rgba(96,165,250,0.95)'
  if (pct == null || pct === 0) return 'rgba(148,163,184,0.45)'
  return pct > 0 ? 'rgba(248,113,113,0.9)' : 'rgba(52,211,153,0.9)'
}

/**
 * 板块实时动能气泡:
 * - 左绿 / 右红双池 (按涨跌幅分流)
 * - 半径 ∝ 成交额
 * - 颜色深浅 ∝ |涨跌幅|
 * - ECharts force 布局做轻微漂移动效
 *
 * 口径: 「涨跌+成交代理动能」, 不是交易所级主力净流入。
 */
export function SectorFlowBubbles({
  items,
  selectedKey = null,
  onSelect,
  title = '实时动能气泡',
  height = 340,
  maxItems = 48,
  className,
}: Props) {
  const [hoverKey, setHoverKey] = useState<string | null>(null)

  const prepared = useMemo(() => {
    const valid = items
      .filter(it => it.count > 0 && Number.isFinite(it.totalAmount))
      .slice()
      .sort((a, b) => b.totalAmount - a.totalAmount || b.heatScore - a.heatScore)
      .slice(0, maxItems)

    const amounts = valid.map(v => Math.max(0, v.totalAmount))
    const maxAmt = Math.max(...amounts, 1)
    const positive = amounts.filter(a => a > 0)
    const minAmt = positive.length ? Math.min(...positive) : maxAmt

    return valid.map((it, idx) => {
      const amt = Math.max(0, it.totalAmount)
      let r = 22
      if (maxAmt > minAmt && amt > 0) {
        const t = (Math.log1p(amt) - Math.log1p(minAmt)) / (Math.log1p(maxAmt) - Math.log1p(minAmt) || 1)
        r = 18 + clamp(t, 0, 1) * 38
      } else if (amt <= 0) {
        r = 16
      }
      const pct = it.avgPct
      const side: 'up' | 'down' | 'flat' =
        pct == null || !Number.isFinite(pct) || pct === 0 ? 'flat' : pct > 0 ? 'up' : 'down'
      // 初始位置: 红右绿左
      const jitterY = ((idx * 37) % 100) / 100 - 0.5
      const jitterX = ((idx * 53) % 100) / 100 - 0.5
      const baseX = side === 'up' ? 72 : side === 'down' ? 28 : 50
      const baseY = 50 + jitterY * 28
      return {
        ...it,
        radius: r,
        side,
        x: baseX + jitterX * 6,
        y: baseY,
      }
    })
  }, [items, maxItems])

  const upCount = prepared.filter(p => p.side === 'up').length
  const downCount = prepared.filter(p => p.side === 'down').length

  const option = useMemo<EChartsOption | null>(() => {
    if (!prepared.length) return null

    const nodes = prepared.map(it => ({
      id: it.key,
      name: it.key,
      value: [
        it.x,
        it.y,
        it.totalAmount,
        it.avgPct ?? 0,
        it.heatScore,
        it.count,
        it.upCount,
        it.downCount,
      ],
      x: it.x,
      y: it.y,
      symbolSize: it.radius * 2,
      category: it.side,
      itemStyle: {
        color: pctColor(it.avgPct),
        borderColor: borderColor(it.avgPct, selectedKey === it.key),
        borderWidth: selectedKey === it.key ? 2.5 : 1.2,
        shadowBlur: selectedKey === it.key ? 18 : 8,
        shadowColor: selectedKey === it.key
          ? 'rgba(96,165,250,0.45)'
          : it.side === 'up'
            ? 'rgba(239,68,68,0.25)'
            : it.side === 'down'
              ? 'rgba(16,185,129,0.25)'
              : 'rgba(148,163,184,0.15)',
      },
      label: {
        show: it.radius >= 22,
        formatter: () => (it.key.length > 6 ? `${it.key.slice(0, 6)}…` : it.key),
        color: '#e2e8f0',
        fontSize: it.radius >= 34 ? 11 : 10,
        fontWeight: 500,
        textBorderColor: 'rgba(15,23,42,0.65)',
        textBorderWidth: 2,
      },
    }))

    return {
      animationDurationUpdate: 700,
      animationEasingUpdate: 'cubicOut',
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15,23,42,0.92)',
        borderColor: 'rgba(148,163,184,0.25)',
        textStyle: { color: '#e2e8f0', fontSize: 12 },
        formatter: (params: any) => {
          const d = params?.data
          if (!d) return ''
          const [, , amount, pct, heat, count, up, down] = d.value as number[]
          const pctText = Number.isFinite(pct) ? fmtPct(pct) : '—'
          const pctCls = pct > 0 ? '#f87171' : pct < 0 ? '#34d399' : '#94a3b8'
          return [
            `<div style="font-weight:600;margin-bottom:4px">${d.name}</div>`,
            `<div>涨跌幅 <span style="color:${pctCls};font-variant-numeric:tabular-nums">${pctText}</span></div>`,
            `<div>成交额 <span style="font-variant-numeric:tabular-nums">${fmtBigNum(amount)}</span></div>`,
            `<div>强度 ${heat.toFixed(0)} · 成分 ${count} · 涨${up}/跌${down}</div>`,
            `<div style="margin-top:4px;color:#94a3b8;font-size:11px">动能代理: 涨跌×成交 (非主力净流入)</div>`,
          ].join('')
        },
      },
      xAxis: { type: 'value', min: 0, max: 100, show: false },
      yAxis: { type: 'value', min: 0, max: 100, show: false },
      series: [
        {
          type: 'graph',
          layout: 'force',
          coordinateSystem: 'cartesian2d',
          roam: true,
          draggable: true,
          force: {
            repulsion: 160,
            gravity: 0.05,
            edgeLength: 36,
            layoutAnimation: true,
            friction: 0.65,
          },
          data: nodes,
          categories: [{ name: 'up' }, { name: 'down' }, { name: 'flat' }],
          emphasis: {
            scale: 1.08,
            itemStyle: { borderWidth: 2.5, shadowBlur: 22 },
            label: { show: true, fontSize: 12 },
          },
        } as any,
      ],
      graphic: [
        {
          type: 'text',
          left: '6%',
          top: '8%',
          style: {
            text: '弱势 / 绿池',
            fill: 'rgba(52,211,153,0.55)',
            fontSize: 12,
            fontWeight: 600,
          },
        },
        {
          type: 'text',
          right: '6%',
          top: '8%',
          style: {
            text: '强势 / 红池',
            fill: 'rgba(248,113,113,0.55)',
            fontSize: 12,
            fontWeight: 600,
          },
        },
      ],
    }
  }, [prepared, selectedKey])

  const chartRef = useECharts(option, [option, selectedKey])

  useEffect(() => {
    if (!chartRef.current) return
    const chart = echarts.getInstanceByDom(chartRef.current)
    if (!chart) return

    const onClick = (params: any) => {
      const key = params?.data?.id ?? params?.data?.name
      if (key && onSelect) onSelect(String(key))
    }
    const onOver = (params: any) => {
      const key = params?.data?.id ?? params?.data?.name
      setHoverKey(key ? String(key) : null)
    }
    const onOut = () => setHoverKey(null)

    chart.on('click', onClick)
    chart.on('mouseover', onOver)
    chart.on('mouseout', onOut)
    return () => {
      chart.off('click', onClick)
      chart.off('mouseover', onOver)
      chart.off('mouseout', onOut)
    }
  }, [chartRef, onSelect, prepared.length, option])

  const active = prepared.find(p => p.key === (hoverKey || selectedKey)) ?? null

  return (
    <div className={cn('rounded-2xl border border-border bg-surface/70 overflow-hidden', className)}>
      <div className="flex items-center justify-between gap-3 border-b border-border/60 px-4 py-2.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-foreground">{title}</span>
            <span className="rounded-full bg-elevated/70 px-2 py-0.5 text-[10px] text-muted">
              动能代理 · 非主力净流入
            </span>
          </div>
          <div className="mt-0.5 text-[11px] text-muted">
            半径=成交额 · 颜色=涨跌幅 · 左绿右红 · 点击选中板块
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[11px] tabular-nums">
          <span className="rounded-md bg-bull/10 px-2 py-1 text-bull">红 {upCount}</span>
          <span className="rounded-md bg-bear/10 px-2 py-1 text-bear">绿 {downCount}</span>
          <span className="rounded-md bg-elevated/70 px-2 py-1 text-muted">{prepared.length} 个</span>
        </div>
      </div>

      {!prepared.length ? (
        <div className="flex items-center justify-center text-sm text-muted" style={{ height }}>
          暂无可用行情聚合（需要 change_pct / 成交额）
        </div>
      ) : (
        <div className="relative">
          <div className="pointer-events-none absolute inset-0 flex">
            <div className="w-1/2 bg-gradient-to-r from-emerald-500/[0.07] to-transparent" />
            <div className="w-1/2 bg-gradient-to-l from-rose-500/[0.07] to-transparent" />
          </div>
          <div ref={chartRef} style={{ height, width: '100%' }} />
          {active && (
            <div className="pointer-events-none absolute bottom-3 left-3 right-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-border/70 bg-base/80 px-3 py-2 text-[11px] backdrop-blur-sm">
              <span className="font-semibold text-foreground">{active.key}</span>
              <span className={cn(
                'font-mono tabular-nums',
                (active.avgPct ?? 0) > 0 ? 'text-bull' : (active.avgPct ?? 0) < 0 ? 'text-bear' : 'text-muted',
              )}>
                {active.avgPct != null ? fmtPct(active.avgPct) : '—'}
              </span>
              <span className="text-muted">成交 {fmtBigNum(active.totalAmount)}</span>
              <span className="text-muted">强度 {active.heatScore.toFixed(0)}</span>
              <span className="text-muted">涨{active.upCount}/跌{active.downCount}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
