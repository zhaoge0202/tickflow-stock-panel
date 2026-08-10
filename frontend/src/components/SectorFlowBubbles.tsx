import { useMemo, useEffect, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/cn'

/** 板块动能气泡的最小数据契约 */
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
  maxItems?: number
  playbackActive?: boolean
  frameKey?: number | string | null
  className?: string
}

const SECTOR_FLOW_CHART_UPDATE_OPTIONS = { notMerge: false, lazyUpdate: false }

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v))
}

function clampSpan(v: number, lo: number, hi: number) {
  if (lo > hi) return (lo + hi) / 2
  return clamp(v, lo, hi)
}

function pctColor(pct: number | null): string {
  if (pct == null || !Number.isFinite(pct) || pct === 0) return 'rgba(148,163,184,0.88)'
  const intensity = clamp(Math.abs(pct) / 0.08, 0.4, 1)
  const a = 0.4 + intensity * 0.5
  if (pct > 0) return `rgba(239, 68, 68, ${a.toFixed(2)})`
  return `rgba(16, 185, 129, ${a.toFixed(2)})`
}

function borderColor(pct: number | null, selected: boolean): string {
  if (selected) return 'rgba(96,165,250,0.95)'
  if (pct == null || pct === 0) return 'rgba(148,163,184,0.5)'
  return pct > 0 ? 'rgba(248,113,113,0.95)' : 'rgba(52,211,153,0.95)'
}

function hashString(value: string) {
  let hash = 0
  for (let i = 0; i < value.length; i++) {
    hash = (hash * 31 + value.charCodeAt(i)) >>> 0
  }
  return hash
}

function frameToNumber(frameKey: number | string | null | undefined) {
  if (typeof frameKey === 'number' && Number.isFinite(frameKey)) {
    return Math.floor(frameKey / 60_000)
  }
  if (typeof frameKey === 'string' && frameKey) {
    return hashString(frameKey)
  }
  return 0
}

function playbackMotion(key: string, frame: number, pulseFrame: number, active: boolean) {
  if (!active) return { dx: 0, dy: 0, scale: 1, opacity: 0.92 }
  const seed = hashString(key)
  const angle = ((seed % 360) + frame * 31 + pulseFrame * 7) * Math.PI / 180
  const crossAngle = ((seed % 211) - frame * 17 + pulseFrame * 5) * Math.PI / 180
  const wave = 0.5 + Math.sin(pulseFrame * 0.72 + seed * 0.017) * 0.5
  const drift = 0.46 + (seed % 7) * 0.06
  return {
    dx: Math.cos(angle) * drift + Math.cos(crossAngle) * 0.18,
    dy: Math.sin(angle) * drift + Math.sin(crossAngle) * 0.18,
    scale: 0.78 + wave * 0.5,
    opacity: 0.84 + wave * 0.16,
  }
}

/**
 * 用简单圆形碰撞把气泡在左右池内摊开, 避免 force 把节点叠成一团。
 * 坐标系: x/y ∈ [0,100] (百分比画布)。
 */
function bubblePool(
  side: 'up' | 'down' | 'flat',
  widthPx: number,
  heightPx: number,
) {
  const marginX = Math.max(14, widthPx * 0.025)
  const gap = Math.max(28, widthPx * 0.045)
  const top = 24
  const bottom = Math.max(top + 120, heightPx - 58)
  if (side === 'up') {
    return { left: widthPx / 2 + gap / 2, right: widthPx - marginX, top, bottom }
  }
  if (side === 'down') {
    return { left: marginX, right: widthPx / 2 - gap / 2, top, bottom }
  }
  return {
    left: widthPx * 0.38,
    right: widthPx * 0.62,
    top: top + 18,
    bottom,
  }
}

function layoutBubbles(
  items: Array<SectorFlowItem & { radius: number; side: 'up' | 'down' | 'flat' }>,
  widthPx: number,
  heightPx: number,
) {
  type Node = {
    key: string
    x: number
    y: number
    r: number
    side: 'up' | 'down' | 'flat'
    item: SectorFlowItem & { radius: number; side: 'up' | 'down' | 'flat' }
  }

  const totals = new Map<'up' | 'down' | 'flat', number>()
  for (const it of items) totals.set(it.side, (totals.get(it.side) ?? 0) + 1)
  const seen = new Map<'up' | 'down' | 'flat', number>()

  const nodes: Node[] = items.map(it => {
    const n = totals.get(it.side) || 1
    const localIdx = seen.get(it.side) ?? 0
    seen.set(it.side, localIdx + 1)
    const pool = bubblePool(it.side, widthPx, heightPx)
    const poolW = Math.max(80, pool.right - pool.left)
    const poolH = Math.max(120, pool.bottom - pool.top)
    const cols = Math.max(1, Math.ceil(Math.sqrt(n * poolW / poolH)))
    const rows = Math.max(1, Math.ceil(n / cols))
    const col = localIdx % cols
    const row = Math.floor(localIdx / cols)
    const cellW = poolW / cols
    const cellH = poolH / rows
    const seed = Array.from(it.key).reduce((sum, ch) => sum + ch.charCodeAt(0), 0)
    const jitterX = (((seed * 37) % 100) / 100 - 0.5) * cellW * 0.18
    const jitterY = (((seed * 53) % 100) / 100 - 0.5) * cellH * 0.18
    return {
      key: it.key,
      x: pool.left + (col + 0.5) * cellW + jitterX,
      y: pool.top + (row + 0.5) * cellH + jitterY,
      r: it.radius,
      side: it.side,
      item: it,
    }
  })

  // 多轮排斥 + 池内约束
  const iterations = 140
  for (let iter = 0; iter < iterations; iter++) {
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]
        const b = nodes[j]
        let dx = b.x - a.x
        let dy = b.y - a.y
        let dist = Math.hypot(dx, dy) || 0.01
        const minDist = a.r + b.r + 4
        if (dist < minDist) {
          const push = (minDist - dist) / 2
          dx /= dist
          dy /= dist
          a.x -= dx * push
          a.y -= dy * push
          b.x += dx * push
          b.y += dy * push
        }
      }
    }
    // 约束在池内
    for (const n of nodes) {
      const pool = bubblePool(n.side, widthPx, heightPx)
      n.x = clampSpan(n.x, pool.left + n.r, pool.right - n.r)
      n.y = clampSpan(n.y, pool.top + n.r, pool.bottom - n.r)
    }
  }

  return nodes.map(n => ({
    ...n,
    x: clamp((n.x / widthPx) * 100, 0, 100),
    y: clamp((n.y / heightPx) * 100, 0, 100),
    item: { ...n.item, radius: n.r },
  }))
}

/**
 * 板块动能气泡 (独立布局, 不重叠):
 * - 左绿 / 右红双池
 * - 半径 ∝ 成交额
 * - 颜色深浅 ∝ |涨跌幅|
 * - 碰撞布局摊开, 避免叠成一团
 */
export function SectorFlowBubbles({
  items,
  selectedKey = null,
  onSelect,
  title = '实时动能气泡',
  height = 420,
  maxItems = 36,
  playbackActive = false,
  frameKey = null,
  className,
}: Props) {
  const [hoverKey, setHoverKey] = useState<string | null>(null)
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const [chartSize, setChartSize] = useState({ width: 900, height })
  const [motionTick, setMotionTick] = useState(0)
  const playbackFrame = frameToNumber(frameKey)
  const shouldAnimatePlayback = playbackActive && frameKey != null

  useEffect(() => {
    if (!shouldAnimatePlayback) {
      setMotionTick(0)
      return
    }
    const timer = window.setInterval(() => {
      setMotionTick(prev => (prev + 1) % 100_000)
    }, 120)
    return () => window.clearInterval(timer)
  }, [shouldAnimatePlayback])

  useEffect(() => {
    const el = chartContainerRef.current
    if (!el) return
    const update = () => {
      const rect = el.getBoundingClientRect()
      const next = {
        width: Math.max(320, Math.round(rect.width || 900)),
        height: Math.max(260, Math.round(rect.height || height)),
      }
      setChartSize(prev => (
        prev.width === next.width && prev.height === next.height ? prev : next
      ))
    }
    update()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update)
      return () => window.removeEventListener('resize', update)
    }
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [height])

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

    const withSide = valid.map(it => {
      const pct = it.avgPct
      const side: 'up' | 'down' | 'flat' =
        pct == null || !Number.isFinite(pct) || pct === 0 ? 'flat' : pct > 0 ? 'up' : 'down'
      return { ...it, side }
    })
    const sideCounts = withSide.reduce<Record<'up' | 'down' | 'flat', number>>((acc, it) => {
      acc[it.side] += 1
      return acc
    }, { up: 0, down: 0, flat: 0 })
    const radiusCap = (side: 'up' | 'down' | 'flat') => {
      const pool = bubblePool(side, chartSize.width, chartSize.height)
      const area = Math.max(1, (pool.right - pool.left) * (pool.bottom - pool.top))
      const count = Math.max(1, sideCounts[side])
      return clamp(Math.sqrt((area * 0.52) / (Math.PI * count)), 13, 54)
    }

    const sized = withSide.map(it => {
      const amt = Math.max(0, it.totalAmount)
      const cap = radiusCap(it.side)
      const minR = Math.min(18, Math.max(11, cap * 0.72))
      let r = Math.min(26, cap)
      if (maxAmt > minAmt && amt > 0) {
        const t = (Math.log1p(amt) - Math.log1p(minAmt)) / (Math.log1p(maxAmt) - Math.log1p(minAmt) || 1)
        r = minR + clamp(t, 0, 1) * (cap - minR)
      } else if (amt <= 0) {
        r = minR
      }
      return { ...it, radius: clamp(r, minR, cap) }
    })

    return layoutBubbles(sized, chartSize.width, chartSize.height)
  }, [items, maxItems, chartSize.width, chartSize.height])

  const upCount = prepared.filter(p => p.side === 'up').length
  const downCount = prepared.filter(p => p.side === 'down').length

  const option = useMemo<EChartsOption | null>(() => {
    if (!prepared.length) return null

    const data = prepared.map(n => {
      const it = n.item
      const motion = playbackMotion(it.key, playbackFrame, motionTick, shouldAnimatePlayback)
      return {
        id: it.key,
        name: it.key,
        value: [
          clamp(n.x + motion.dx, 2, 98),
          clamp(n.y + motion.dy, 2, 98),
          it.totalAmount,
          it.avgPct ?? 0,
          it.heatScore,
          it.count,
          it.upCount,
          it.downCount,
        ],
        symbolSize: Math.max(10, it.radius * 2 * motion.scale),
        itemStyle: {
          color: pctColor(it.avgPct),
          borderColor: borderColor(it.avgPct, selectedKey === it.key),
          borderWidth: selectedKey === it.key ? 3 : 1.5,
          shadowBlur: selectedKey === it.key ? 16 : shouldAnimatePlayback ? 10 : 6,
          shadowColor: selectedKey === it.key
            ? 'rgba(96,165,250,0.5)'
            : it.side === 'up'
              ? 'rgba(239,68,68,0.28)'
              : it.side === 'down'
                ? 'rgba(16,185,129,0.28)'
                : 'rgba(148,163,184,0.18)',
          opacity: motion.opacity,
        },
        label: {
          show: it.radius >= 22,
          formatter: () => (it.key.length > 5 ? `${it.key.slice(0, 5)}…` : it.key),
          color: '#f8fafc',
          fontSize: it.radius >= 32 ? 11 : 10,
          fontWeight: 600,
          textBorderColor: 'rgba(15,23,42,0.75)',
          textBorderWidth: 2,
        },
      }
    })

    return {
      animation: true,
      animationDuration: 450,
      animationDurationUpdate: shouldAnimatePlayback ? 110 : 450,
      animationEasingUpdate: shouldAnimatePlayback ? 'linear' : 'cubicOut',
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15,23,42,0.94)',
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
      grid: { left: 0, right: 0, top: 0, bottom: 0 },
      xAxis: { type: 'value', min: 0, max: 100, show: false, minInterval: 1 },
      yAxis: { type: 'value', min: 0, max: 100, show: false, minInterval: 1 },
      series: [
        {
          type: 'scatter',
          data,
          symbol: 'circle',
          animationThreshold: 10_000,
          animationDelayUpdate: shouldAnimatePlayback ? (idx: number) => Math.min(idx * 2, 60) : 0,
          // 禁止重叠时 ECharts 自动挪点 (我们自己做了碰撞)
          large: false,
          emphasis: {
            scale: 1.12,
            itemStyle: { borderWidth: 3, shadowBlur: 20 },
            label: { show: true, fontSize: 12 },
          },
        },
      ],
      graphic: [
        {
          type: 'text',
          left: '4%',
          top: '4%',
          style: { text: '弱势 · 绿池', fill: 'rgba(52,211,153,0.65)', fontSize: 12, fontWeight: 700 },
        },
        {
          type: 'text',
          right: '4%',
          top: '4%',
          style: { text: '强势 · 红池', fill: 'rgba(248,113,113,0.65)', fontSize: 12, fontWeight: 700 },
        },
        // 中线
        {
          type: 'line',
          shape: { x1: 0, y1: 0, x2: 0, y2: height },
          left: '50%',
          top: 0,
          style: { stroke: 'rgba(148,163,184,0.18)', lineWidth: 1, lineDash: [4, 4] },
        },
      ],
    }
  }, [prepared, selectedKey, height, playbackFrame, motionTick, shouldAnimatePlayback])

  const chartRef = useECharts(
    option,
    [option, selectedKey, playbackFrame, motionTick, shouldAnimatePlayback],
    chartContainerRef,
    SECTOR_FLOW_CHART_UPDATE_OPTIONS,
  )

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

  const active = prepared.find(p => p.key === (hoverKey || selectedKey))?.item ?? null

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
            半径=成交额 · 颜色=涨跌幅 · 左绿右红 · 点击选中
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
            <div className="w-1/2 bg-gradient-to-r from-emerald-500/[0.08] via-emerald-500/[0.03] to-transparent" />
            <div className="w-1/2 bg-gradient-to-l from-rose-500/[0.08] via-rose-500/[0.03] to-transparent" />
          </div>
          <div ref={chartRef} style={{ height, width: '100%' }} />
          {active && (
            <div className="pointer-events-none absolute bottom-3 left-3 right-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-border/70 bg-base/85 px-3 py-2 text-[11px] backdrop-blur-sm">
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
