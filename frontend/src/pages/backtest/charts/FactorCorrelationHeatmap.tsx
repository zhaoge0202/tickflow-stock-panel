import { useEffect, useMemo, useState } from 'react'
import type { EChartsOption } from 'echarts'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from './useECharts'

export interface FactorCorrelationHeatmapProps {
  labels: string[]
  matrix: (number | null)[][]
  pairCounts?: (number | null)[][]
  threshold?: number
}

interface HeatmapDatum {
  value: [number, number, number, number | null]
  itemStyle?: { opacity: number }
}

interface PreparedHeatmap {
  labels: string[]
  data: HeatmapDatum[]
}

const EMPTY_HEATMAP: PreparedHeatmap = { labels: [], data: [] }

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  })[char] ?? char)
}

export function FactorCorrelationHeatmap({
  labels,
  matrix,
  pairCounts,
  threshold,
}: FactorCorrelationHeatmapProps) {
  const ct = useChartTheme()
  const [prepared, setPrepared] = useState<PreparedHeatmap>(EMPTY_HEATMAP)

  // Flattening can dominate render time for wide factor sets, so keep it out of render.
  useEffect(() => {
    if (!labels.length || !matrix.length) {
      setPrepared(EMPTY_HEATMAP)
      return
    }

    const nextLabels = labels.slice()
    const data: HeatmapDatum[] = []
    const thresholdValue = typeof threshold === 'number' && Number.isFinite(threshold)
      ? Math.min(1, Math.max(0, Math.abs(threshold)))
      : null

    for (let rowIndex = 0; rowIndex < nextLabels.length; rowIndex += 1) {
      const row = matrix[rowIndex]
      if (!row) continue

      for (let columnIndex = 0; columnIndex < nextLabels.length; columnIndex += 1) {
        const rho = row[columnIndex]
        if (typeof rho !== 'number' || !Number.isFinite(rho)) continue

        const rawPairCount = pairCounts?.[rowIndex]?.[columnIndex]
        const pairCount = typeof rawPairCount === 'number' && Number.isFinite(rawPairCount)
          ? rawPairCount
          : null
        const passesThreshold = thresholdValue == null
          || rowIndex === columnIndex
          || Math.abs(rho) >= thresholdValue

        data.push({
          value: [columnIndex, rowIndex, rho, pairCount],
          ...(passesThreshold ? {} : { itemStyle: { opacity: 0.3 } }),
        })
      }
    }

    setPrepared({ labels: nextLabels, data })
  }, [labels, matrix, pairCounts, threshold])

  const option = useMemo<EChartsOption | null>(() => {
    if (!prepared.data.length) return null

    return {
      animation: false,
      grid: { left: 78, right: 16, top: 14, bottom: 72 },
      tooltip: {
        trigger: 'item',
        confine: true,
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (params: any) => {
          const value = params.value as HeatmapDatum['value'] | undefined
          if (!value) return ''
          const [columnIndex, rowIndex, rho, pairCount] = value
          const rowLabel = escapeHtml(prepared.labels[rowIndex] ?? '')
          const columnLabel = escapeHtml(prepared.labels[columnIndex] ?? '')
          return `<div style="font-size:11px;color:${ct.text};margin-bottom:5px">${rowLabel} × ${columnLabel}</div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>rho</span><span style="font-family:monospace">${rho.toFixed(4)}</span></div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>配对数</span><span style="font-family:monospace">${pairCount == null ? '—' : pairCount.toLocaleString('zh-CN')}</span></div>`
        },
      },
      xAxis: {
        type: 'category',
        data: prepared.labels,
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          interval: 0,
          rotate: prepared.labels.length > 6 ? 40 : 0,
          width: 68,
          overflow: 'truncate',
        },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
        splitArea: { show: false },
      },
      yAxis: {
        type: 'category',
        data: prepared.labels,
        inverse: true,
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          width: 64,
          overflow: 'truncate',
        },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
        splitArea: { show: false },
      },
      visualMap: {
        type: 'continuous',
        min: -1,
        max: 1,
        dimension: 2,
        orient: 'horizontal',
        left: 'center',
        bottom: 4,
        itemWidth: 100,
        itemHeight: 8,
        calculable: false,
        precision: 1,
        text: ['1', '-1'],
        textGap: 6,
        textStyle: { color: ct.text, fontSize: 10 },
        inRange: {
          color: ['#2563eb', ct.fillSubtle, '#ef4444'],
        },
      },
      series: [{
        name: '相关性',
        type: 'heatmap',
        data: prepared.data,
        progressive: 2000,
        itemStyle: {
          borderColor: ct.border,
          borderWidth: 1,
        },
        emphasis: {
          itemStyle: {
            borderColor: ct.textStrong,
            borderWidth: 1,
          },
        },
      }],
    }
  }, [prepared, ct])

  const chartRef = useECharts(option, [prepared, ct])
  const isEmpty = prepared.data.length === 0

  return (
    <div className="relative h-[360px] w-full min-w-0 overflow-hidden">
      <div
        ref={chartRef}
        className="h-full w-full min-w-0"
        role="img"
        aria-label="因子相关性热力图"
      />
      {isEmpty && (
        <div className="absolute inset-0 flex items-center justify-center bg-surface text-xs text-secondary">
          暂无相关性数据
        </div>
      )}
    </div>
  )
}
