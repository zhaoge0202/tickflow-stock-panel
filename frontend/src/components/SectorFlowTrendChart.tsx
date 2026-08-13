import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import type { SectorFlowSeriesItem, SectorFlowSeriesResponse } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { cn } from '@/lib/cn'
import { fmtBigNum, priceColorClass } from '@/lib/format'

const COLORS = [
  '#F97316', '#EF4444', '#8B5CF6', '#EAB308', '#10B981',
  '#38BDF8', '#EC4899', '#A3A3A3', '#14B8A6', '#F43F5E',
]

type Props = {
  data?: SectorFlowSeriesResponse
  metric: 'strength' | 'main_flow'
  selectedKeys: string[]
  onToggle: (key: string) => void
  onSelectAll: (keys: string[]) => void
  isLoading: boolean
  isFetching: boolean
  error: Error | null
  onRefresh: () => void
  search?: string
  playbackActive?: boolean
  playbackTs?: number | null
}

function fmtTime(ts: number) {
  return new Date(ts).toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function fmtFlow(value: number | null) {
  if (value == null || !Number.isFinite(value)) return '—'
  return fmtBigNum(value)
}

function seriesValues(item: SectorFlowSeriesItem, metric: Props['metric']) {
  return metric === 'main_flow' ? item.flow_values : item.strength_values
}

function displayValue(item: SectorFlowSeriesItem, metric: Props['metric']) {
  return metric === 'main_flow' ? item.latest_flow : item.latest_strength
}

function sourceLabel(item: SectorFlowSeriesItem) {
  if (item.flow_source === 'trade_ticks') return '逐笔'
  if (item.flow_source === 'active_volume_estimate') return '盘口估算'
  return '动能估算'
}

function buildOption(
  data: SectorFlowSeriesResponse,
  metric: Props['metric'],
  selected: SectorFlowSeriesItem[],
  ct: ReturnType<typeof useChartTheme>,
): EChartsOption {
  const xAxis = data.points.map(fmtTime)
  const lineSeries = selected.map((item, index) => ({
    name: item.name,
    type: 'line' as const,
    yAxisIndex: metric === 'main_flow' ? 0 : 1,
    showSymbol: false,
    smooth: 0.16,
    connectNulls: false,
    lineStyle: { width: 1.8, color: COLORS[index % COLORS.length] },
    itemStyle: { color: COLORS[index % COLORS.length] },
    data: seriesValues(item, metric),
  }))
  const indexSeries = {
    name: data.index.name,
    type: 'line' as const,
    yAxisIndex: 1,
    showSymbol: false,
    smooth: 0.12,
    connectNulls: false,
    lineStyle: { width: 1.4, type: 'dashed' as const, color: '#A1A1AA' },
    itemStyle: { color: '#A1A1AA' },
    data: data.index.values.map(value => value == null ? null : value * 100),
  }
  return {
    animation: false,
    color: COLORS,
    grid: { left: 62, right: 62, top: 34, bottom: 34 },
    legend: {
      top: 0,
      left: 0,
      right: 0,
      type: 'scroll',
      textStyle: { color: ct.text, fontSize: 11 },
      itemWidth: 18,
      itemHeight: 2,
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: ct.tooltipBg,
      borderColor: ct.border,
      textStyle: { color: ct.text, fontSize: 11 },
      valueFormatter: value => {
        const number = Number(value)
        if (!Number.isFinite(number)) return '—'
        return metric === 'main_flow' ? fmtFlow(number) : `${number.toFixed(2)}`
      },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: xAxis,
      axisLine: { lineStyle: { color: ct.border } },
      axisLabel: { color: ct.text, fontSize: 10, hideOverlap: true },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        name: metric === 'main_flow' ? '净流入' : '强度',
        nameTextStyle: { color: ct.text, fontSize: 10 },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          formatter: (value: number) => metric === 'main_flow' ? fmtFlow(value) : value.toFixed(0),
        },
        splitLine: { lineStyle: { color: ct.grid, type: 'dashed' } },
        axisLine: { show: false },
      },
      {
        type: 'value',
        name: '上证涨跌幅',
        nameTextStyle: { color: ct.text, fontSize: 10 },
        axisLabel: { color: ct.text, fontSize: 10, formatter: (value: number) => `${value.toFixed(2)}%` },
        splitLine: { show: false },
        axisLine: { show: false },
      },
    ],
    series: [...lineSeries, indexSeries],
  }
}

export function SectorFlowTrendChart({
  data,
  metric,
  selectedKeys,
  onToggle,
  onSelectAll,
  isLoading,
  isFetching,
  error,
  onRefresh,
  search = '',
  playbackActive = false,
  playbackTs = null,
}: Props) {
  const chartEl = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const ct = useChartTheme()
  const allSectors = data?.sectors ?? []
  const sectors = useMemo(() => {
    const query = search.trim().toLowerCase()
    return query ? allSectors.filter(item => item.name.toLowerCase().includes(query)) : allSectors
  }, [allSectors, search])
  const selected = useMemo(() => {
    const chosen = new Set(selectedKeys)
    return allSectors.filter(item => chosen.has(item.key))
  }, [allSectors, selectedKeys])
  const visibleData = useMemo(() => {
    if (playbackTs == null) return data
    const endIndex = data?.points.findLastIndex(point => point <= playbackTs) ?? -1
    const count = endIndex + 1
    if (!data || count <= 0 || count >= data.points.length) return data
    return {
      ...data,
      points: data.points.slice(0, count),
      sectors: data.sectors.map(item => ({
        ...item,
        flow_values: item.flow_values.slice(0, count),
        strength_values: item.strength_values.slice(0, count),
      })),
      index: {
        ...data.index,
        values: data.index.values.slice(0, count),
      },
    }
  }, [data, playbackTs])
  const visibleSelected = useMemo(() => {
    const chosen = new Set(selectedKeys)
    return visibleData?.sectors.filter(item => chosen.has(item.key)) ?? []
  }, [selectedKeys, visibleData])

  useEffect(() => {
    const el = chartEl.current
    if (!visibleData || !el) return
    const chart = chartRef.current ?? echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    chart.setOption(buildOption(visibleData, metric, visibleSelected, ct), true)
    const resize = () => chart.resize()
    const observer = new ResizeObserver(resize)
    observer.observe(el)
    return () => observer.disconnect()
  }, [visibleData, metric, visibleSelected, ct])

  useEffect(() => () => {
    chartRef.current?.dispose()
    chartRef.current = null
  }, [])

  const selectAll = () => onSelectAll(sectors.slice(0, 10).map(item => item.key))

  if (isLoading && !data) {
    return (
      <div className="flex h-[34rem] items-center justify-center rounded-lg border border-border bg-surface text-sm text-muted">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
        正在生成板块曲线，首次读取当天行情可能需要一点时间
      </div>
    )
  }
  if (error) {
    return (
      <div className="flex h-[34rem] flex-col items-center justify-center rounded-lg border border-border bg-surface text-sm text-muted">
        <AlertTriangle className="mb-2 h-5 w-5 text-bear" />
        <span>板块曲线加载失败</span>
        <button onClick={onRefresh} className="mt-3 inline-flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-xs text-foreground hover:bg-elevated">
          <RefreshCw className="h-3.5 w-3.5" /> 重试
        </button>
      </div>
    )
  }
  if (!data || sectors.length === 0 || data.points.length === 0) {
    return (
      <div className="flex h-[34rem] items-center justify-center rounded-lg border border-border bg-surface text-sm text-muted">
        当前日期没有可用的板块盘中数据
      </div>
    )
  }

  return (
    <section className="grid min-h-[34rem] grid-cols-[15rem_minmax(0,1fr)] overflow-hidden rounded-lg border border-border bg-surface/80">
      <aside className="min-h-0 border-r border-border/70">
        <div className="flex items-center justify-between border-b border-border/70 px-3 py-3">
          <div>
            <div className="text-sm font-semibold text-foreground">板块列表</div>
            <div className="mt-0.5 text-[10px] text-muted">{sectors.length} 个可用板块</div>
          </div>
          <button onClick={selectAll} className="text-[10px] text-accent hover:text-foreground">前10</button>
        </div>
        <div className="max-h-[30rem] overflow-auto">
          {sectors.map(item => {
            const active = selectedKeys.includes(item.key)
            const value = displayValue(item, metric)
            return (
              <button
                key={item.key}
                onClick={() => onToggle(item.key)}
                className={cn(
                  'grid w-full grid-cols-[1rem_minmax(0,1fr)_4.5rem] items-center gap-2 border-b border-border/40 px-3 py-2 text-left text-xs hover:bg-elevated/60',
                  active && 'bg-elevated/70',
                )}
              >
                <span className={cn('h-3.5 w-3.5 border', active ? 'border-accent bg-accent' : 'border-muted')} />
                <span className="min-w-0 truncate text-foreground">{item.name}</span>
                <span className={cn('truncate text-right font-mono tabular-nums', metric === 'main_flow' ? (value != null && value >= 0 ? 'text-bull' : 'text-bear') : priceColorClass(value))}>
                  {metric === 'main_flow' ? fmtFlow(value) : value == null ? '—' : `${value.toFixed(1)}`}
                </span>
              </button>
            )
          })}
        </div>
      </aside>
      <div className="min-w-0 p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted">
          <div className="flex items-center gap-3">
            <span>{metric === 'main_flow' ? '累计净流入曲线' : '板块强度曲线'}</span>
            <span className="text-secondary">虚线：上证指数涨跌幅</span>
            {playbackActive && visibleData && visibleData.points.length > 0 && (
              <span className="text-accent">
                回放 {fmtTime(visibleData.points[visibleData.points.length - 1])}
              </span>
            )}
            {data.data_quality.is_proxy && <span className="text-amber-500">当前为估算值 · 非正式主力净流入</span>}
            {data.data_quality.status === 'incomplete' && (
              <span className="text-red-500">历史快照不完整，不可用于资金流判断</span>
            )}
            {data.data_quality.max_gap_seconds != null && data.data_quality.max_gap_seconds > 125 && (
              <span className="text-amber-500">
                数据存在 {Math.round(data.data_quality.max_gap_seconds / 60)} 分钟断档，曲线已断开
              </span>
            )}
          </div>
          <button onClick={onRefresh} disabled={isFetching} className="p-1 text-muted hover:text-foreground disabled:opacity-50" title="刷新">
            <RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} />
          </button>
        </div>
        <div ref={chartEl} className="h-[30rem] w-full" />
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted">
          {selected.map(item => (
            <span key={item.key}>
              {item.name} · {sourceLabel(item)} · 覆盖 {Math.round(item.coverage_ratio * 100)}%
            </span>
          ))}
          {data.data_quality.point_coverage_ratio != null && (
            <span>快照完整度 {Math.round(data.data_quality.point_coverage_ratio * 100)}%</span>
          )}
        </div>
      </div>
    </section>
  )
}
