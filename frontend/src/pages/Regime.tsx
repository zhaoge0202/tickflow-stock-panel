/**
 * 市场环境(Regime)页 — 每日环境状态时序趋势 + 状态分布。
 *
 * 数据来源: 后端 regime_builder 批算的时序表(每日离散状态 + 多维指标)。
 * 不复刻 Dashboard 的当日总览(那是单日快照), 聚焦历史趋势与状态分布。
 *
 * 时间范围: 1年(250交易日) / 2年(500) / 自定义(1~1000天) / 全部(走日期范围)。
 * 美化对齐 Dashboard 设计语言: 半透明 surface 卡片 + 渐变竖条标题 + 语义色。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import {
  Activity, RefreshCw, Loader2, Gauge, TrendingUp,
  Flame, BarChart3, Pencil,
} from 'lucide-react'
import {
  api, type RegimeRow, type RegimeState,
  REGIME_STATE_LABELS, REGIME_STATE_COLORS,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { fmtBigNum } from '@/lib/format'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'

const STATE_ORDER: RegimeState[] = ['strong', 'lean_strong', 'range', 'lean_weak', 'weak']

// ── 时间范围 ──────────────────────────────────────────────
// 1年=250 交易日, 2年=500 交易日; 自定义 1~1000; 全部走 start/end 日期范围。
type RangePreset = '1y' | '2y' | 'all' | { custom: number }

const RANGE_LABEL: Record<'1y' | '2y' | 'all', string> = {
  '1y': '1年', '2y': '2年', all: '全部',
}

/** 把 preset 解析成 (start?, end?, limit?) 三元组供 history 接口使用。 */
function resolveHistoryRange(
  preset: RangePreset,
  coverage: { earliest_date: string | null; latest_date: string | null } | undefined,
): { start?: string; end?: string; limit?: number } {
  if (preset === '1y') return { limit: 250 }
  if (preset === '2y') return { limit: 500 }
  if (preset === 'all') {
    // 全部: 用 coverage 实际日期范围, 不传 limit
    return { start: coverage?.earliest_date ?? undefined, end: coverage?.latest_date ?? undefined }
  }
  // 自定义天数
  return { limit: Math.max(1, Math.min(1000, preset.custom)) }
}

/** history/states 共用的"天数"语义: 用于 states 接口 + 标题展示。 */
function resolveDays(
  preset: RangePreset,
  coverage: { rows: number } | undefined,
): number {
  if (preset === '1y') return 250
  if (preset === '2y') return 500
  if (preset === 'all') return coverage?.rows && coverage.rows > 0 ? coverage.rows : 1000
  return Math.max(1, Math.min(1000, preset.custom))
}

function isPresetKey(p: RangePreset, k: '1y' | '2y' | 'all'): boolean {
  return p === k
}

// ── EChart hook ───────────────────────────────────────────
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

// ── 页内通用 SectionTitle (对齐 Dashboard 渐变竖条风格) ────
function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-accent to-accent/30" />
      <Icon className="h-3.5 w-3.5 text-accent" />
      <h2 className="text-xs font-semibold text-foreground">{title}</h2>
      {hint != null && <span className="ml-auto text-[10px] text-muted font-mono">{hint}</span>}
    </div>
  )
}

// ── 卡片容器样式 (Dashboard 同款) ─────────────────────────
const cardCls = 'rounded-card border border-border bg-surface/80 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]'

// ── 主组件 ────────────────────────────────────────────────
export function Regime() {
  const qc = useQueryClient()
  const [range, setRange] = useState<RangePreset>('1y')
  const [customOpen, setCustomOpen] = useState(false)
  const ct = useChartTheme()

  // coverage: "全部"模式 + 标题展示依赖
  const coverage = useQuery({
    queryKey: QK.regimeCoverage,
    queryFn: () => api.regimeCoverage(),
    staleTime: 5 * 60 * 1000,
  })

  const days = resolveDays(range, coverage.data)
  const histRange = resolveHistoryRange(range, coverage.data)

  // queryKey 用 range 的完整三元组区分: limit / start+end(全部) / custom天数
  const history = useQuery({
    queryKey: ['regime-history', range] as const,
    queryFn: () => api.regimeHistory(histRange.start, histRange.end, histRange.limit),
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
      const r = await api.regimeRecompute()
      toast(r.computed > 0 ? `重算完成 · 新增 ${r.computed} 天` : '重算完成 · 数据已是最新', 'success')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['regime-history'] }),
        qc.invalidateQueries({ queryKey: ['regime-states'] }),
        qc.invalidateQueries({ queryKey: ['regime-latest'] }),
        qc.invalidateQueries({ queryKey: QK.regimeCoverage }),
      ])
    } catch (e) {
      toast(`重算失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setRecomputing(false)
    }
  }

  // 自定义按钮标签
  const customLabel = typeof range === 'object'
    ? `自定义 ${range.custom}天`
    : '自定义'

  return (
    <div className="mx-auto max-w-6xl px-4 py-5 space-y-4">
      {/* ── 头部 (Dashboard 渐变条卡片) ── */}
      <div className={cn(cardCls, 'relative overflow-hidden rounded-card bg-gradient-to-r from-surface/90 to-surface/70 px-4 py-3')}>
        <div className="absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" />
        <div className="flex items-center gap-3">
          <Activity className="h-5 w-5 text-accent" />
          <h1 className="text-base font-semibold text-foreground">市场环境</h1>
          <span className="text-xs text-muted">每日环境状态 · 赚钱效应 · 趋势分析</span>
          <div className="ml-auto flex items-center gap-2">
            {/* 时间范围按钮组 */}
            <div className="flex items-center rounded-btn border border-border bg-base/60 p-0.5">
              {(['1y', '2y', 'all'] as const).map(k => (
                <button
                  key={k}
                  onClick={() => setRange(k)}
                  className={cn(
                    'h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                    isPresetKey(range, k)
                      ? 'bg-accent text-white shadow-sm'
                      : 'text-secondary hover:text-foreground',
                  )}
                >
                  {RANGE_LABEL[k]}
                </button>
              ))}
              <button
                onClick={() => setCustomOpen(true)}
                className={cn(
                  'inline-flex items-center gap-1 h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                  typeof range === 'object'
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-secondary hover:text-foreground',
                )}
              >
                {typeof range === 'object' && <Pencil className="h-3 w-3" />}
                {customLabel}
              </button>
            </div>
            {/* 重算 */}
            <button onClick={handleRecompute} disabled={recomputing}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent disabled:opacity-50">
              {recomputing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {recomputing ? '重算中…' : '重算'}
            </button>
          </div>
        </div>
      </div>

      {/* ── 最新日概览 (4 个指标卡) ── */}
      {latest ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {/* 状态卡 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Gauge className="h-3 w-3" /> 最新状态 · {latest.date}
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-2xl font-bold" style={{ color: REGIME_STATE_COLORS[latest.state] }}>
                {REGIME_STATE_LABELS[latest.state]}
              </span>
              <span className="text-sm text-muted">{latest.score} 分</span>
            </div>
            {/* 评分进度条 0~100 */}
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${Math.max(2, Math.min(100, latest.score))}%`, backgroundColor: REGIME_STATE_COLORS[latest.state] }} />
            </div>
          </div>

          {/* 涨停 / 跌停 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Flame className="h-3 w-3" /> 涨停 / 跌停
            </div>
            <div className="mt-1.5 flex items-baseline gap-1 text-lg font-semibold">
              <span className="text-bull">{latest.limit_up}</span>
              <span className="mx-0.5 text-muted">/</span>
              <span className="text-bear">{latest.limit_down}</span>
            </div>
            <div className="mt-1 text-[10px] text-muted">连板高度 {latest.max_consecutive} · 封板率 {(latest.seal_rate * 100).toFixed(0)}%</div>
          </div>

          {/* 涨跌家数比 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <TrendingUp className="h-3 w-3" /> 涨跌家数比
            </div>
            <div className="mt-1.5 text-lg font-semibold text-foreground">{latest.up_ratio.toFixed(2)}</div>
            <div className="mt-1 flex items-center gap-1.5 text-[10px]">
              <span className="text-bull">涨 {latest.up_count}</span>
              <span className="text-muted">·</span>
              <span className="text-bear">跌 {latest.down_count}</span>
            </div>
          </div>

          {/* 成交额 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <BarChart3 className="h-3 w-3" /> 成交额
            </div>
            <div className="mt-1.5 text-lg font-semibold text-foreground">{fmtBigNum(latest.total_amount)}</div>
            <div className="mt-2 flex items-center gap-1.5">
              <span className="text-[10px] text-muted">MA20 上方 {(latest.above_ma20_pct * 100).toFixed(0)}%</span>
              <div className="ml-auto h-1.5 w-12 overflow-hidden rounded-full bg-base">
                <div className="h-full rounded-full bg-accent"
                  style={{ width: `${Math.max(0, Math.min(100, latest.above_ma20_pct * 100))}%` }} />
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
          {history.isLoading ? '加载中…' : '暂无环境数据，请先运行盘后管道或点击「重算」'}
        </div>
      )}

      {/* ── 状态色带时间轴 ── */}
      {rows.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Activity} title="状态时间轴"
            hint={`${rows[0]?.date} → ${rows[rows.length - 1]?.date} · ${rows.length} 天`} />
          <div className="mt-2.5 flex h-7 w-full overflow-hidden rounded-md">
            {rows.map(r => (
              <div key={r.date} title={`${r.date} ${REGIME_STATE_LABELS[r.state]}(${r.score})`}
                className="flex-1 min-w-[2px] transition-opacity hover:opacity-80"
                style={{ backgroundColor: REGIME_STATE_COLORS[r.state] }} />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted">
            {STATE_ORDER.map(s => (
              <span key={s} className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded"
                  style={{ backgroundColor: REGIME_STATE_COLORS[s] }} />
                {REGIME_STATE_LABELS[s]}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ── 趋势图 + 分布图 ── */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className={cn(cardCls, 'p-3 lg:col-span-2')}>
          <SectionTitle icon={Activity} title="环境综合分 · 涨停数趋势" />
          <div ref={trendRef} className="mt-2 h-[320px]" />
        </div>
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Gauge} title="状态分布" hint={`近 ${days} 天`} />
          <div ref={pieRef} className="mt-2 h-[320px]" />
        </div>
      </div>

      {/* ── 自定义天数弹窗 ── */}
      {customOpen && (
        <CustomDaysModal
          current={typeof range === 'object' ? range.custom : 120}
          onClose={() => setCustomOpen(false)}
          onApply={(d) => { setRange({ custom: d }); setCustomOpen(false) }}
        />
      )}
    </div>
  )
}

// ── 自定义天数输入弹窗 ────────────────────────────────────
function CustomDaysModal({ current, onClose, onApply }: {
  current: number
  onClose: () => void
  onApply: (days: number) => void
}) {
  const [val, setVal] = useState(String(current))
  const inputRef = useRef<HTMLInputElement>(null)

  const apply = () => {
    const n = Math.max(1, Math.min(1000, Math.floor(Number(val) || 0)))
    if (Number.isNaN(n) || n < 1) {
      toast('请输入 1 ~ 1000 之间的天数', 'error')
      return
    }
    onApply(n)
  }

  return (
    <Modal onClose={onClose} ariaLabel="自定义天数" initialFocusRef={inputRef}
      panelClassName="w-[88vw] max-w-xs bg-surface border border-border rounded-card shadow-xl p-4">
      <div className="space-y-3">
        <div>
          <div className="text-xs font-medium text-foreground">自定义天数</div>
          <div className="mt-0.5 text-[10px] text-muted">范围 1 ~ 1000 个交易日</div>
        </div>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={1000}
          value={val}
          onChange={e => setVal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') apply() }}
          className="h-8 w-full rounded-input border border-border bg-base px-2.5 text-sm text-foreground outline-none focus:border-accent"
        />
        {/* 快捷预设 */}
        <div className="flex flex-wrap gap-1.5">
          {[60, 90, 180, 365].map(d => (
            <button key={d} onClick={() => setVal(String(d))}
              className="h-6 rounded-btn border border-border bg-base px-2 text-[11px] text-secondary hover:text-accent hover:border-accent/40 transition-colors">
              {d}天
            </button>
          ))}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose}
            className="h-7 rounded-btn px-3 text-xs text-secondary hover:text-foreground transition-colors">
            取消
          </button>
          <button onClick={apply}
            className="h-7 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 transition-colors">
            应用
          </button>
        </div>
      </div>
    </Modal>
  )
}
