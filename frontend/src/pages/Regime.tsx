/**
 * 市场环境(Regime)页 — 每日环境状态时序趋势 + 状态分布。
 *
 * 数据来源: 后端 regime_builder 批算的时序表(每日离散状态 + 多维指标)。
 * 不复刻 Dashboard 的当日总览(那是单日快照), 聚焦历史趋势与状态分布。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Activity, RefreshCw, Loader2 } from 'lucide-react'
import {
  api, type RegimeRow, type RegimeState,
  REGIME_STATE_LABELS, REGIME_STATE_COLORS,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { fmtBigNum } from '@/lib/format'

const STATE_ORDER: RegimeState[] = ['strong', 'lean_strong', 'range', 'lean_weak', 'weak']

function useEChart(option: echarts.EChartsOption | null, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    if (!ref.current) return
    instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])
  useEffect(() => {
    if (instRef.current && option) instRef.current.setOption(option, { notMerge: true })
  }, [option, ...deps])
  return ref
}

export function Regime() {
  const qc = useQueryClient()
  const [days, setDays] = useState(120)
  const ct = useChartTheme()

  const history = useQuery({
    queryKey: QK.regimeHistory(days),
    queryFn: () => api.regimeHistory(undefined, undefined, days),
    staleTime: 5 * 60 * 1000,
  })
  const states = useQuery({
    queryKey: QK.regimeStates(days),
    queryFn: () => api.regimeStates(days),
    staleTime: 5 * 60 * 1000,
  })
  const [recomputing, setRecomputing] = useState(false)

  const rows: RegimeRow[] = history.data?.rows ?? []
  const latest = rows.length > 0 ? rows[rows.length - 1] : null

  // 趋势图: 综合分曲线 + 涨停数柱状
  const trendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (rows.length === 0) return null
    const dates = rows.map(r => r.date)
    const scores = rows.map(r => r.score)
    const limitUps = rows.map(r => r.limit_up)
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText } },
      legend: { data: ['综合分', '涨停数'], textStyle: { color: ct.text }, top: 0 },
      grid: { left: 48, right: 48, top: 32, bottom: 56 },
      xAxis: {
        type: 'category', data: dates,
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: string) => v.slice(5) },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '综合分', min: 0, max: 100, axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
        { type: 'value', name: '涨停', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
      ],
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - (60 / days) * 100) },
        { type: 'slider', bottom: 8, height: 16, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        { name: '综合分', type: 'line', data: scores, smooth: true, symbol: 'none',
          lineStyle: { width: 2, color: ct.textStrong }, areaStyle: { opacity: 0.08 },
          markLine: { silent: true, lineStyle: { type: 'dashed', color: ct.grid }, data: [
            { yAxis: 75, label: { formatter: '强势', color: ct.text, fontSize: 9 } },
            { yAxis: 40, label: { formatter: '震荡', color: ct.text, fontSize: 9 } },
          ] } },
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 1, barMaxWidth: 6, itemStyle: { color: REGIME_STATE_COLORS.strong } },
      ],
    }
  }, [rows, days, ct])
  const trendRef = useEChart(trendOption, [trendOption])

  // 状态分布饼图
  const pieOption = useMemo<echarts.EChartsOption | null>(() => {
    const dist = states.data?.distribution ?? []
    if (dist.length === 0) return null
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText } },
      series: [{
        type: 'pie', radius: ['42%', '70%'], center: ['50%', '52%'],
        label: { color: ct.text, fontSize: 10, formatter: '{b}\n{d}%' },
        data: STATE_ORDER
          .map(s => dist.find(d => d.state === s))
          .filter((x): x is NonNullable<typeof x> => !!x)
          .map(d => ({
            name: d.label, value: d.count,
            itemStyle: { color: REGIME_STATE_COLORS[d.state] },
          })),
      }],
    }
  }, [states.data, ct])
  const pieRef = useEChart(pieOption, [pieOption])

  const handleRecompute = async () => {
    setRecomputing(true)
    try {
      await api.regimeRecompute()
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['regime-history'] }),
        qc.invalidateQueries({ queryKey: ['regime-states'] }),
        qc.invalidateQueries({ queryKey: ['regime-latest'] }),
      ])
    } finally {
      setRecomputing(false)
    }
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-5 space-y-4">
      {/* 头部 */}
      <div className="flex items-center gap-3">
        <Activity className="h-5 w-5 text-accent" />
        <h1 className="text-base font-semibold text-foreground">市场环境</h1>
        <span className="text-xs text-muted">每日环境状态 · 赚钱效应 · 趋势分析</span>
        <div className="ml-auto flex items-center gap-2">
          <select value={days} onChange={e => setDays(Number(e.target.value))}
            className="h-7 rounded-btn border border-border bg-base px-2 text-xs text-foreground">
            <option value={60}>近 60 天</option>
            <option value={120}>近 120 天</option>
            <option value={250}>近 250 天</option>
          </select>
          <button onClick={handleRecompute} disabled={recomputing}
            className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent disabled:opacity-50">
            {recomputing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            重算
          </button>
        </div>
      </div>

      {/* 最新日概览 */}
      {latest ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-card border border-border bg-base p-3">
            <div className="text-[10px] text-muted">最新状态 · {latest.date}</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="text-2xl font-bold" style={{ color: REGIME_STATE_COLORS[latest.state] }}>
                {REGIME_STATE_LABELS[latest.state]}
              </span>
              <span className="text-sm text-muted">{latest.score} 分</span>
            </div>
          </div>
          <div className="rounded-card border border-border bg-base p-3">
            <div className="text-[10px] text-muted">涨停 / 跌停</div>
            <div className="mt-1 text-lg font-semibold text-foreground">
              <span className="text-red-400">{latest.limit_up}</span>
              <span className="mx-1 text-muted">/</span>
              <span className="text-green-400">{latest.limit_down}</span>
            </div>
            <div className="text-[10px] text-muted">连板高度 {latest.max_consecutive} · 封板率 {(latest.seal_rate * 100).toFixed(0)}%</div>
          </div>
          <div className="rounded-card border border-border bg-base p-3">
            <div className="text-[10px] text-muted">涨跌家数比</div>
            <div className="mt-1 text-lg font-semibold text-foreground">{latest.up_ratio.toFixed(2)}</div>
            <div className="text-[10px] text-muted">涨 {latest.up_count} · 跌 {latest.down_count}</div>
          </div>
          <div className="rounded-card border border-border bg-base p-3">
            <div className="text-[10px] text-muted">成交额</div>
            <div className="mt-1 text-lg font-semibold text-foreground">{fmtBigNum(latest.total_amount)}</div>
            <div className="text-[10px] text-muted">MA20 上方 {(latest.above_ma20_pct * 100).toFixed(0)}%</div>
          </div>
        </div>
      ) : (
        <div className="rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
          {history.isLoading ? '加载中…' : '暂无环境数据，请先运行盘后管道或点击「重算」'}
        </div>
      )}

      {/* 状态色带 */}
      {rows.length > 0 && (
        <div className="rounded-card border border-border bg-base p-3">
          <div className="mb-2 text-xs font-medium text-foreground">状态时间轴</div>
          <div className="flex h-6 w-full overflow-hidden rounded">
            {rows.map(r => (
              <div key={r.date} title={`${r.date} ${REGIME_STATE_LABELS[r.state]}(${r.score})`}
                className="flex-1 min-w-[2px]" style={{ backgroundColor: REGIME_STATE_COLORS[r.state] }} />
            ))}
          </div>
          <div className="mt-1.5 flex items-center gap-3 text-[10px] text-muted">
            <span>{rows[0]?.date}</span>
            <div className="ml-auto flex items-center gap-2">
              {STATE_ORDER.map(s => (
                <span key={s} className="flex items-center gap-1">
                  <span className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: REGIME_STATE_COLORS[s] }} />
                  {REGIME_STATE_LABELS[s]}
                </span>
              ))}
            </div>
            <span>{rows[rows.length - 1]?.date}</span>
          </div>
        </div>
      )}

      {/* 趋势图 + 分布图 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-card border border-border bg-base p-3 lg:col-span-2">
          <div className="mb-1 text-xs font-medium text-foreground">环境综合分 · 涨停数趋势</div>
          <div ref={trendRef} className="h-[320px]" />
        </div>
        <div className="rounded-card border border-border bg-base p-3">
          <div className="mb-1 text-xs font-medium text-foreground">状态分布（近 {days} 天）</div>
          <div ref={pieRef} className="h-[320px]" />
        </div>
      </div>
    </div>
  )
}
