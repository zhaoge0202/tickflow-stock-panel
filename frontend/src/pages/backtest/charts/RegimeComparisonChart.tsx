import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from './useECharts'

export interface RegimeComparisonRow {
  state: string
  label: string
  nDates: number
  sharpe: number | null
  return: number | null
  maxDrawdown: number | null
}

export interface RegimeComparisonChartProps {
  rows: RegimeComparisonRow[]
}

function finiteOrNull(value: number | null): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  })[char] ?? char)
}

export function RegimeComparisonChart({ rows }: RegimeComparisonChartProps) {
  const ct = useChartTheme()
  const preparedRows = useMemo(() => rows.map(row => {
    const hasEnoughSamples = Number.isFinite(row.nDates) && row.nDates >= 2
    return {
      ...row,
      hasEnoughSamples,
      sharpe: hasEnoughSamples ? finiteOrNull(row.sharpe) : null,
      return: hasEnoughSamples ? finiteOrNull(row.return) : null,
      maxDrawdown: hasEnoughSamples ? finiteOrNull(row.maxDrawdown) : null,
    }
  }), [rows])

  const hasMetrics = preparedRows.some(row => (
    row.return != null || row.sharpe != null || row.maxDrawdown != null
  ))

  const option = useMemo<EChartsOption | null>(() => {
    if (!preparedRows.length || !hasMetrics) return null

    return {
      animation: false,
      color: ['#ef4444', '#22c55e', '#3b82f6'],
      grid: { left: 54, right: 48, top: 48, bottom: 48 },
      legend: {
        top: 2,
        left: 'center',
        itemWidth: 12,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { color: ct.text, fontSize: 10 },
        data: ['收益', '最大回撤', 'Sharpe'],
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'shadow' },
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (params: any) => {
          const items = Array.isArray(params) ? params : [params]
          const index = items[0]?.dataIndex as number | undefined
          const row = index == null ? undefined : preparedRows[index]
          if (!row) return ''

          const state = row.state && row.state !== row.label
            ? `<span style="color:${ct.text}">${escapeHtml(row.state)}</span>`
            : ''
          const status = row.hasEnoughSamples ? '' : '<div style="margin-top:4px">样本不足</div>'
          return `<div style="font-size:11px;margin-bottom:5px">${escapeHtml(row.label)} ${state}</div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>交易日</span><span style="font-family:monospace">${row.nDates.toLocaleString('zh-CN')}</span></div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>收益</span><span style="font-family:monospace">${row.return == null ? '—' : `${(row.return * 100).toFixed(2)}%`}</span></div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>最大回撤</span><span style="font-family:monospace">${row.maxDrawdown == null ? '—' : `${(row.maxDrawdown * 100).toFixed(2)}%`}</span></div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>Sharpe</span><span style="font-family:monospace">${row.sharpe == null ? '—' : row.sharpe.toFixed(2)}</span></div>${status}`
        },
      },
      xAxis: {
        type: 'category',
        data: preparedRows.map(row => row.label),
        axisLabel: {
          interval: 0,
          fontSize: 10,
          width: 72,
          overflow: 'truncate',
          formatter: (_value: string, index: number) => preparedRows[index]?.hasEnoughSamples
            ? `{normal|${preparedRows[index]?.label ?? ''}}`
            : `{muted|${preparedRows[index]?.label ?? ''}}`,
          rich: {
            normal: { color: ct.text },
            muted: { color: ct.text, opacity: 0.45 },
          },
        },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: 'value',
          name: '比例',
          nameTextStyle: { color: ct.text, fontSize: 10 },
          axisLabel: {
            color: ct.text,
            fontSize: 10,
            formatter: (value: number) => `${value.toFixed(0)}%`,
          },
          axisLine: { show: false },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          type: 'value',
          name: 'Sharpe',
          nameTextStyle: { color: ct.text, fontSize: 10 },
          axisLabel: { color: ct.text, fontSize: 10 },
          axisLine: { show: false },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: '收益',
          type: 'bar',
          data: preparedRows.map(row => row.return == null ? null : row.return * 100),
          barMaxWidth: 24,
          itemStyle: { color: '#ef4444' },
        },
        {
          name: '最大回撤',
          type: 'bar',
          data: preparedRows.map(row => row.maxDrawdown == null ? null : row.maxDrawdown * 100),
          barMaxWidth: 24,
          itemStyle: { color: '#22c55e' },
        },
        {
          name: 'Sharpe',
          type: 'line',
          yAxisIndex: 1,
          data: preparedRows.map(row => row.sharpe),
          connectNulls: false,
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { color: '#3b82f6', width: 1.5 },
          itemStyle: { color: '#3b82f6' },
          z: 5,
        },
      ],
    }
  }, [preparedRows, hasMetrics, ct])

  const chartRef = useECharts(option, [preparedRows, ct])
  const emptyMessage = rows.length === 0
    ? '暂无市场状态数据'
    : '样本不足，暂无可比较指标'

  return (
    <div className="relative h-[304px] w-full min-w-0 overflow-hidden">
      <div
        ref={chartRef}
        className="h-full w-full min-w-0"
        role="img"
        aria-label="市场状态指标比较图"
      />
      {!hasMetrics && (
        <div className="absolute inset-0 flex items-center justify-center bg-surface text-xs text-secondary">
          {emptyMessage}
        </div>
      )}
    </div>
  )
}
