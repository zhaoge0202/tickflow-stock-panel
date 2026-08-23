import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { X, RefreshCw, Clock, LineChart, Star, RadioTower, Maximize2, Minimize2, Activity } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { cnSignal } from '@/lib/signals'
import { fmtPct } from '@/lib/format'
import { StockPanel, getDefaultRange } from '@/components/StockPanel'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import { StockMultiDayIntradayChart } from '@/components/StockMultiDayIntradayChart'
import { DatePicker } from '@/components/DatePicker'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { PriceAlertDialog } from '@/components/stock-analysis/PriceAlertDialog'
import { buildMonitorPriceLines } from '@/lib/price-alerts'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'
import { setFocusSymbol, clearFocusSymbol } from '@/lib/useQuoteStream'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { storage } from '@/lib/storage'
import { ExtensionSlot } from '@/extensions/ExtensionSlot'

interface Props {
  symbol: string | null
  name?: string
  onClose: () => void
  /** 触发信息 (来自监控触发记录, 有值时在顶栏下方显示) */
  triggerInfo?: {
    price?: number | null
    changePct?: number | null
    ts?: number
    signals?: string[]
    message?: string
  } | null
}

// ===== 板块标识（与 Screener 列表一致）=====

// 预设快捷范围（只保留半年和1年）
const PRESETS: { label: string; months: number }[] = [
  { label: '半年', months: 6 },
  { label: '1年', months: 12 },
]

type PreviewView = 'daily' | 'intraday'
interface PriceAlertDraft {
  id: number
  targetPrice: number
  currentPrice: number
}
const INTRADAY_DAY_OPTIONS = [1, 5, 10, 20] as const

function loadIntradayDays(): number {
  const saved = storage.stockPreviewIntradayDays.get(10)
  return INTRADAY_DAY_OPTIONS.includes(saved as typeof INTRADAY_DAY_OPTIONS[number])
    ? saved
    : 10
}

function boardTag(symbol: string): { label: string; color: string } | null {
  if (/^(300|301)/.test(symbol)) return { label: '创', color: 'text-[#f97316] bg-[#f97316]/12 border-[#f97316]/25' }
  if (/^688/.test(symbol))       return { label: '科', color: 'text-purple-400 bg-purple-400/12 border-purple-400/25' }
  if (/^[48]/.test(symbol))      return { label: '北', color: 'text-cyan-400 bg-cyan-400/12 border-cyan-400/25' }
  return null
}

// ===== 异动边缘 (与异动页同口径) =====

const AB_STATUS_META: Record<string, { label: string; cls: string; bar: string; icon: string }> = {
  triggered: { label: '已触发', cls: 'bg-danger/20 text-danger font-semibold', bar: 'border-b border-danger/30 bg-danger/[0.08]', icon: 'text-danger' },
  edge: { label: '异动边缘', cls: 'bg-warning/20 text-warning font-semibold', bar: 'border-b border-warning/30 bg-warning/[0.07]', icon: 'text-warning' },
  watch: { label: '观察', cls: 'bg-elevated text-secondary font-semibold', bar: 'border-b border-border bg-surface', icon: 'text-secondary' },
}

/** 异动引擎计算时间 (服务端 asof 秒级时间戳 → 月-日 时:分:秒) */
function fmtAbnormalCalcTime(asofSec: number): string {
  const d = new Date(asofSec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function StockPreviewDialog({ symbol, name, onClose, triggerInfo }: Props) {
  const [view, setView] = useState<PreviewView>('daily')
  const [intradayDays, setIntradayDays] = useState(loadIntradayDays)
  const [dateRange, setDateRange] = useState(getDefaultRange)
  const [showMonitorEditor, setShowMonitorEditor] = useState(false)
  const [priceAlertDraft, setPriceAlertDraft] = useState<PriceAlertDraft | null>(null)
  const [maximized, setMaximized] = useState(false)
  const qc = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)

  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: !!symbol,
  })
  const monitorRules = useQuery({
    queryKey: QK.monitorRules,
    queryFn: api.monitorRulesList,
    enabled: !!symbol,
  })
  // 异动边缘: 与异动页同 queryKey 共享缓存; 该股处于观察/边缘/触发状态时在图表上方显示信息条
  const abnormal = useQuery({
    queryKey: QK.abnormalOverview(0.5, 300),
    queryFn: () => api.abnormalOverview(0.5, 300),
    enabled: !!symbol,
  })
  const abRow = symbol
    ? abnormal.data?.rows.find(r => r.symbol === symbol)
    : undefined
  // 接近度最高的窗口 (信息条中高亮)
  const abDominantWindow = abRow
    ? Object.entries(abRow.windows).reduce(
        (best, [k, w]) => (!best || w.closeness > best[1].closeness ? [k, w] as const : best),
        undefined as undefined | readonly [string, { value: number; threshold: number; closeness: number }],
      )
    : undefined
  const monitorPriceLines = useMemo(
    () => symbol ? buildMonitorPriceLines(monitorRules.data?.rules ?? [], symbol) : [],
    [monitorRules.data?.rules, symbol],
  )
  const inWatchlist = (watchlist.data?.symbols ?? []).some((s: any) => s.symbol === symbol)

  const toggleWatchlist = useMutation({
    mutationFn: ({
      action,
      groupId,
    }: {
      action: 'add' | 'remove'
      groupId?: string | null
    }) => action === 'remove'
      ? api.watchlistRemove(symbol!)
      : api.watchlistAdd(symbol!, '', groupId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  // ESC 关闭
  useEffect(() => {
    if (!symbol) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !priceAlertDraft) onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [symbol, onClose, priceAlertDraft])

  useEffect(() => {
    if (symbol) setView('daily')
    setPriceAlertDraft(null)
  }, [symbol])

  // 焦点股票注册: SSE quotes_updated 推送时精准 invalidate 当前股票日K,
  // 让对话框日K最后一根蜡烛随实时价变化 (后端只读内存, 不调 TickFlow)。
  // 关闭/切股时清除, 避免无谓刷新。
  useEffect(() => {
    if (!symbol) return
    setFocusSymbol(symbol)
    return () => clearFocusSymbol()
  }, [symbol])

  // 分时图实时轮询: 复用自选列表的「分时刷新开关 + 间隔」偏好。
  // 仅实时行情运行 且 用户开启分时刷新时才轮询; 否则 undefined (定格)。
  const { data: prefs } = usePreferences()
  const { data: quoteStatus } = useQuoteStatus()
  const realtimeRunning = quoteStatus?.running ?? false
  const intradayRefreshOn = prefs?.minute_intraday_refresh ?? false
  const intradayRefetchMs = (intradayRefreshOn && realtimeRunning)
    ? (prefs?.minute_intraday_refresh_interval ?? 6) * 1000
    : undefined

  const handleRefresh = () => {
    if (!symbol) return
    if (view === 'daily') {
      qc.invalidateQueries({ queryKey: ['kline', symbol] })
    } else {
      qc.invalidateQueries({ queryKey: ['kline-minute-range', symbol] })
      qc.invalidateQueries({ queryKey: ['kline-minute', symbol!] })
    }
  }

  const selectIntradayDays = (days: number) => {
    setIntradayDays(days)
    storage.stockPreviewIntradayDays.set(days)
  }

  const openPriceAlert = (targetPrice: number, currentPrice: number) => {
    setPriceAlertDraft({ id: Date.now(), targetPrice, currentPrice })
  }

  return (
    <AnimatePresence>
      {symbol && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* 遮罩 */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            {...backdrop}
          />

          {/* 弹窗主体 */}
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              'relative rounded-card border border-border bg-base shadow-2xl overflow-hidden flex flex-col transition-all duration-200 ease-smooth',
              maximized ? 'w-screen h-screen max-w-none max-h-none' : 'w-[92vw] max-w-[1100px] max-h-[95vh]',
            )}
          >
            {/* 顶栏 */}
            <div className="flex items-center justify-between gap-3 px-4 py-3 sm:px-5 shrink-0">
              <div className="flex min-w-0 items-center gap-2">
                {(() => {
                  const board = symbol ? boardTag(symbol) : null
                  return board ? (
                    <span className={`inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
                <span className="shrink-0 font-mono text-sm font-medium text-foreground">{symbol}</span>
                {name && <span className="truncate text-xs text-muted">{name}</span>}
              </div>

              <div className="flex shrink-0 items-center gap-1">
                {/* 区间选择 — 随视图切换 */}
                {view === 'daily' ? (
                  <div className="flex items-center gap-1">
                    {PRESETS.map(p => {
                      const now = new Date()
                      const s = new Date(now)
                      s.setMonth(s.getMonth() - p.months)
                      const expected = s.toISOString().slice(0, 10)
                      const isActive = dateRange.start === expected
                      return (
                        <button
                          key={p.label}
                          onClick={() => {
                            const end = new Date().toISOString().slice(0, 10)
                            const ns = new Date()
                            ns.setMonth(ns.getMonth() - p.months)
                            setDateRange({ start: ns.toISOString().slice(0, 10), end })
                          }}
                          className={`h-6 px-1.5 rounded text-[11px] transition-colors cursor-pointer
                            ${isActive
                              ? 'bg-accent/20 text-accent font-medium border border-accent/30'
                              : 'text-muted hover:text-foreground hover:bg-elevated border border-transparent'
                            }`}
                        >
                          {p.label}
                        </button>
                      )
                    })}
                    <DatePicker
                      value={dateRange.start}
                      onChange={(v) => setDateRange(prev => ({ ...prev, start: v }))}
                      max={dateRange.end}
                    />
                    <span className="text-muted/40 text-[10px]">~</span>
                    <DatePicker
                      value={dateRange.end}
                      onChange={(v) => setDateRange(prev => ({ ...prev, end: v }))}
                      min={dateRange.start}
                    />
                  </div>
                ) : (
                  <div className="flex items-center gap-1">
                    <div className="inline-flex shrink-0 items-center rounded border border-border bg-elevated p-0.5" aria-label="分时周期">
                      {INTRADAY_DAY_OPTIONS.map(days => (
                        <button
                          key={days}
                          type="button"
                          aria-pressed={intradayDays === days}
                          onClick={() => selectIntradayDays(days)}
                          className={`h-5 rounded px-1.5 font-mono text-[10px] transition-colors ${
                            intradayDays === days
                              ? 'bg-accent/20 text-accent'
                              : 'text-muted hover:text-secondary'
                          }`}
                        >
                          {days}日
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                <span className="mx-0.5 h-4 w-px shrink-0 bg-border" />

                {/* 日K / 分时 切换 */}
                <div role="tablist" aria-label="图表视图" className="inline-flex shrink-0 items-center rounded border border-border bg-elevated p-0.5">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={view === 'daily'}
                    onClick={() => setView('daily')}
                    className={`inline-flex h-6 items-center gap-1 rounded px-2 text-[11px] transition-colors ${
                      view === 'daily' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                    }`}
                  >
                    <LineChart className="h-3 w-3" />
                    日 K
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={view === 'intraday'}
                    onClick={() => setView('intraday')}
                    className={`inline-flex h-6 items-center gap-1 rounded px-2 text-[11px] transition-colors ${
                      view === 'intraday' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                    }`}
                  >
                    <Clock className="h-3 w-3" />
                    分时
                  </button>
                </div>

                <span className="mx-0.5 h-4 w-px shrink-0 bg-border" />

                {/* 自选 */}
                {inWatchlist ? (
                  <button
                    type="button"
                    onClick={() => toggleWatchlist.mutate({ action: 'remove' })}
                    disabled={toggleWatchlist.isPending}
                    className="rounded-btn p-1.5 text-[#FACC15] transition-colors cursor-pointer hover:bg-elevated disabled:opacity-50"
                    title="移出自选"
                    aria-label={`将 ${symbol} 移出自选`}
                  >
                    <Star className="h-4 w-4" />
                  </button>
                ) : (
                  <WatchlistAddMenu
                    onSelect={groupId => toggleWatchlist.mutate({ action: 'add', groupId })}
                    disabled={toggleWatchlist.isPending}
                    triggerClassName="rounded-btn p-1.5 text-muted transition-colors cursor-pointer hover:bg-elevated hover:text-foreground disabled:opacity-50"
                    ariaLabel={`将 ${symbol} 加入自选`}
                  >
                    <Star className="h-4 w-4" />
                  </WatchlistAddMenu>
                )}
                {/* 加监控 */}
                <button
                  onClick={() => setShowMonitorEditor(true)}
                  className="p-1.5 rounded-btn text-amber-400 hover:bg-amber-400/10 transition-colors cursor-pointer"
                  title="加监控"
                >
                  <RadioTower className="h-4 w-4" />
                </button>

                {/* 刷新 */}
                <button
                  onClick={handleRefresh}
                  className="p-1.5 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors"
                  title="刷新"
                >
                  <RefreshCw className="h-4 w-4" />
                </button>

                {/* 放大 / 缩小 */}
                <button
                  onClick={() => setMaximized(v => !v)}
                  className="p-1.5 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors"
                  title={maximized ? '缩小' : '放大'}
                >
                  {maximized ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
                </button>

                <button
                  onClick={onClose}
                  className="shrink-0 rounded-btn p-1.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                  aria-label="关闭个股详情"
                  title="关闭"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            {/* 触发信息条 (来自监控触发记录) */}
            {triggerInfo && (
              <div className="flex items-center gap-4 border-b border-amber-400/20 bg-amber-400/[0.06] px-5 py-2 shrink-0">
                {/* 左: 触发标记 + 时间 */}
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] font-semibold text-amber-400">⚡ 触发</span>
                  {triggerInfo.ts && (
                    <span className="text-[11px] text-secondary font-mono">
                      {new Date(triggerInfo.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                    </span>
                  )}
                </div>

                {/* 中: 价格 + 涨跌幅 */}
                <div className="flex items-center gap-2 shrink-0">
                  {triggerInfo.price != null && (
                    <span className="text-[11px] font-mono text-foreground/80">{triggerInfo.price.toFixed(2)}</span>
                  )}
                  {triggerInfo.changePct != null && (
                    <span className={`text-[11px] font-mono font-medium ${triggerInfo.changePct >= 0 ? 'text-danger' : 'text-bear'}`}>
                      {triggerInfo.changePct >= 0 ? '+' : ''}{(triggerInfo.changePct * 100).toFixed(2)}%
                    </span>
                  )}
                </div>

                {/* 右: 消息 + 信号标签 */}
                <div className="flex items-center gap-2 flex-wrap min-w-0">
                  {triggerInfo.message && (
                    <span className="text-[11px] text-foreground/70 truncate">{triggerInfo.message}</span>
                  )}
                  {triggerInfo.signals && triggerInfo.signals.length > 0 && (
                    <div className="flex items-center gap-1 flex-wrap">
                      {triggerInfo.signals.map((s, j) => (
                        <span key={j} className="rounded bg-accent/10 px-1.5 py-0.5 text-[9px] text-accent/80">{cnSignal(s)}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* 异动边缘信息条 (与异动页同源; 该股无异动数据时不显示)。整条按状态着色提升辨识度 */}
            {abRow && (() => {
              const meta = AB_STATUS_META[abRow.status] ?? AB_STATUS_META.watch
              return (
                <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-2 shrink-0 ${meta.bar}`}>
                  <span className="flex shrink-0 items-center gap-1.5">
                    <Activity className={`h-3.5 w-3.5 ${meta.icon}`} />
                    <span className={`text-[11px] font-bold ${meta.icon}`}>异动</span>
                    <span className={`rounded px-1.5 py-0.5 text-[10px] ${meta.cls}`}>
                      {meta.label}
                    </span>
                  </span>
                  {Object.entries(abRow.windows)
                    .sort((a, b) => parseInt(a[0], 10) - parseInt(b[0], 10))
                    .map(([w, info]) => {
                      const dominant = abDominantWindow?.[0] === w
                      return (
                        <span
                          key={w}
                          title={`近${parseInt(w, 10)}日累计偏离(含实时) / 交易所规则阈值 · 接近度=|偏离|/阈值`}
                          className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[11px] ${
                            dominant
                              ? 'border-border bg-elevated font-semibold text-foreground'
                              : 'border-border/60 bg-base/40 text-secondary'
                          }`}
                        >
                          {parseInt(w, 10)}日{' '}
                          <span className={info.value >= 0 ? 'text-bull' : 'text-bear'}>{fmtPct(info.value, 1)}</span>
                          <span className="text-muted"> / ±{(info.threshold * 100).toFixed(0)}%</span>
                          <span className="text-muted"> · 接近{(info.closeness * 100).toFixed(0)}%</span>
                        </span>
                      )
                    })}
                  <span
                    className="ml-auto shrink-0 font-mono text-[10px] text-muted"
                    title="异动引擎上次计算时间"
                  >
                    计算于 {fmtAbnormalCalcTime(abnormal.data?.asof ?? 0)}
                  </span>
                </div>
              )
            })()}

            {/* 图表内容 */}
            <div className="flex-1 overflow-auto p-4">
              {view === 'daily' ? (
                <StockPanel
                  symbol={symbol}
                  height={420}
                  showIntraday
                  dateRange={dateRange}
                  priceLines={monitorPriceLines}
                  onPriceDoubleClick={openPriceAlert}
                />
              ) : (
                <>
                <StockPanel
                  symbol={symbol}
                  dateRange={dateRange}
                  infoBarOnly
                />
                <StockMultiDayIntradayChart
                  symbol={symbol}
                  days={intradayDays}
                  height={480}
                  refetchIntervalMs={intradayRefetchMs}
                  priceLines={monitorPriceLines}
                  onPriceDoubleClick={openPriceAlert}
                />
                </>
              )}
            </div>

            {/* 扩展插槽: 对话框底部二开区 (无注册时不渲染) */}
            <div className="shrink-0">
              <ExtensionSlot
                name="stock-preview.footer"
                context={{ symbol, name: name ?? null, view }}
              />
            </div>

            {/* 加监控编辑器弹层 */}
            <AnimatePresence>
              {showMonitorEditor && symbol && (
                <motion.div
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="absolute inset-0 z-20 flex items-start justify-center overflow-auto bg-black/40 p-4"
                  onClick={() => setShowMonitorEditor(false)}
                >
                  <div className="mt-8 w-full max-w-2xl" onClick={e => e.stopPropagation()}>
                    <RuleEditor
                      rule={null}
                      simple
                      preset={{
                        scope: 'symbols',
                        symbols: [symbol],
                        type: 'signal',
                        logic: 'or',
                      }}
                      onClose={() => setShowMonitorEditor(false)}
                      onSaved={() => setShowMonitorEditor(false)}
                    />
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </motion.div>
        </div>
      )}
      {symbol && priceAlertDraft && (
        <PriceAlertDialog
          key={`${symbol}-${priceAlertDraft.id}`}
          symbol={symbol}
          name={name ?? ''}
          initialTarget={priceAlertDraft.targetPrice}
          initialCurrentPrice={priceAlertDraft.currentPrice}
          onClose={() => setPriceAlertDraft(null)}
        />
      )}
    </AnimatePresence>
  )
}
