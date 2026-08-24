import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from './useECharts'

export interface MiningOosFold {
  fold: number | string
  label: string
  return: number | null
  sharpe: number | null
  skipped?: boolean
  reason?: string
}

export interface MiningOosChartProps {
  folds: MiningOosFold[]
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

export function MiningOosChart({ folds }: MiningOosChartProps) {
  const ct = useChartTheme()
  const preparedFolds = useMemo(() => folds.map(fold => ({
    ...fold,
    return: fold.skipped ? null : finiteOrNull(fold.return),
    sharpe: fold.skipped ? null : finiteOrNull(fold.sharpe),
  })), [folds])

  const hasDisplayData = preparedFolds.some(fold => (
    fold.skipped || fold.return != null || fold.sharpe != null
  ))

  const option = useMemo<EChartsOption | null>(() => {
    if (!preparedFolds.length || !hasDisplayData) return null

    return {
      animation: false,
      grid: { left: 54, right: 48, top: 48, bottom: 52 },
      legend: {
        top: 2,
        left: 'center',
        itemWidth: 12,
        itemHeight: 8,
        itemGap: 14,
        textStyle: { color: ct.text, fontSize: 10 },
        data: ['样本外收益', 'Sharpe', '已跳过'],
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
          const skippedItem = items.find(item => Array.isArray(item.value) && typeof item.value[2] === 'number')
          const index = skippedItem
            ? skippedItem.value[2] as number
            : items[0]?.dataIndex as number | undefined
          const fold = index == null ? undefined : preparedFolds[index]
          if (!fold) return ''

          const heading = fold.label || `Fold ${fold.fold}`
          if (fold.skipped) {
            const reason = fold.reason ? escapeHtml(fold.reason) : '未提供原因'
            return `<div style="font-size:11px;margin-bottom:5px">${escapeHtml(heading)}</div>
              <div style="color:${ct.text}">已跳过</div>
              <div style="max-width:260px;white-space:normal;margin-top:4px">${reason}</div>`
          }

          return `<div style="font-size:11px;margin-bottom:5px">${escapeHtml(heading)}</div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>样本外收益</span><span style="font-family:monospace">${fold.return == null ? '—' : `${(fold.return * 100).toFixed(2)}%`}</span></div>
            <div style="display:flex;justify-content:space-between;gap:18px"><span>Sharpe</span><span style="font-family:monospace">${fold.sharpe == null ? '—' : fold.sharpe.toFixed(2)}</span></div>`
        },
      },
      xAxis: {
        type: 'category',
        data: preparedFolds.map(fold => fold.label || `Fold ${fold.fold}`),
        axisLabel: {
          interval: 0,
          fontSize: 10,
          width: 68,
          overflow: 'truncate',
          formatter: (_value: string, index: number) => preparedFolds[index]?.skipped
            ? `{skipped|${preparedFolds[index]?.label || `Fold ${preparedFolds[index]?.fold ?? ''}`}}`
            : `{normal|${preparedFolds[index]?.label || `Fold ${preparedFolds[index]?.fold ?? ''}`}}`,
          rich: {
            normal: { color: ct.text },
            skipped: { color: ct.text, opacity: 0.5 },
          },
        },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: 'value',
          name: '收益',
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
          name: '样本外收益',
          type: 'bar',
          data: preparedFolds.map(fold => fold.return == null
            ? null
            : {
                value: fold.return * 100,
                itemStyle: { color: fold.return >= 0 ? '#ef4444' : '#22c55e' },
              }),
          barMaxWidth: 28,
        },
        {
          name: 'Sharpe',
          type: 'line',
          yAxisIndex: 1,
          data: preparedFolds.map(fold => fold.sharpe),
          connectNulls: false,
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { color: '#3b82f6', width: 1.5 },
          itemStyle: { color: '#3b82f6' },
          z: 5,
        },
        {
          name: '已跳过',
          type: 'custom',
          data: preparedFolds.flatMap((fold, index) => fold.skipped ? [[index, 0, index]] : []),
          renderItem: (params: any, api: any) => {
            const x = api.coord([api.value(0), 0])[0]
            const coordinateSystem = params.coordSys as { y: number }
            return {
              type: 'text',
              x,
              y: coordinateSystem.y + 7,
              style: {
                text: '跳过',
                fill: ct.text,
                opacity: 0.65,
                fontSize: 10,
                align: 'center',
                verticalAlign: 'top',
              },
            }
          },
          z: 10,
        },
      ],
    }
  }, [preparedFolds, hasDisplayData, ct])

  const chartRef = useECharts(option, [preparedFolds, ct])
  const emptyMessage = folds.length === 0
    ? '暂无样本外验证数据'
    : '暂无可展示的样本外指标'

  return (
    <div className="relative h-[304px] w-full min-w-0 overflow-hidden">
      <div
        ref={chartRef}
        className="h-full w-full min-w-0"
        role="img"
        aria-label="逐折样本外收益和 Sharpe 图"
      />
      {!hasDisplayData && (
        <div className="absolute inset-0 flex items-center justify-center bg-surface text-xs text-secondary">
          {emptyMessage}
        </div>
      )}
    </div>
  )
}
