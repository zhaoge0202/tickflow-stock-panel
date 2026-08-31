import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ScanSearch, Clock, TrendingUp, Star, Filter, Layers, Network, Sparkles, RefreshCw, Settings2, Store, RotateCcw, X, Info, History, ChevronLeft, ChevronRight } from 'lucide-react'
import { api, genRuleId, type ScreenerStrategy, type ScreenerResult, type StrategyHistoryEvent, type StrategyPurchaseMark } from '@/lib/api'
import { DEFAULT_STRATEGY_NOTIFY_EVENTS } from '@/lib/strategyMonitorEvents'
import { toast } from '@/components/Toast'
import { useDataStatus, usePreferences, useCapabilities, useQuoteStatus } from '@/lib/useSharedQueries'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import { useStrategyPool } from '@/lib/useStrategyPool'
import { StrategyCard, CardSize, loadCardSize, cardWrapCls } from '@/components/screener/StrategyCard'
import { ScreenerTable } from '@/components/screener/ScreenerTable'
import { ScreenerFilter as ScreenerFilterType, defaultFilter, filterActive, countActiveFilters, applyFilter, FilterPanel } from '@/components/screener/ScreenerFilter'
import { StrategySettingsDialog } from '@/components/screener/StrategySettingsDialog'
import { StrategyPoolDialog } from '@/components/screener/StrategyPoolDialog'
import { StrategyBuilderDialog } from '@/components/screener/StrategyBuilderDialog'
import { StrategyStoreDialog } from '@/components/screener/StrategyStoreDialog'
import { CompositeStrategyDialog } from '@/components/screener/CompositeStrategyDialog'
import { Modal } from '@/components/Modal'
import { ListColumnCustomizer } from '@/components/ListColumnCustomizer'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { resolveCandleConfig } from '@/lib/list-columns'
import {
  SCREENER_BUILTIN_COLUMNS,
  SCREENER_COLUMN_GROUPS,
  buildExtColumnsParam,
  loadScreenerColumnConfig,
  saveScreenerColumnConfig,
  type ColumnConfig,
} from '@/lib/screener-columns'

const AUCTION_GATE_MINUTES = 9 * 60 + 25
const AUCTION_PREWARM_MINUTES = 9 * 60 + 24
const AUCTION_CLOSE_MINUTES = 9 * 60 + 30
const AUCTION_CONFIRM_GRACE_MINUTES = 9 * 60 + 35
const CN_TZ = 'Asia/Shanghai'

function getCnNowMinutes(now = new Date()): number {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: CN_TZ,
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  }).formatToParts(now)
  const hour = Number(parts.find(p => p.type === 'hour')?.value ?? '0')
  const minute = Number(parts.find(p => p.type === 'minute')?.value ?? '0')
  return hour * 60 + minute
}

function getCnTodayIso(now = new Date()): string {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: CN_TZ }).format(now)
}

function formatHistoryTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return '—'
  return new Date(ts).toLocaleTimeString('zh-CN', {
    timeZone: CN_TZ,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function nextBusinessDateIso(dateIso: string): string {
  const [year, month, day] = dateIso.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1, day + 1))
  while (date.getUTCDay() === 0 || date.getUTCDay() === 6) {
    date.setUTCDate(date.getUTCDate() + 1)
  }
  return date.toISOString().slice(0, 10)
}

function isBusinessDateIso(dateIso: string): boolean {
  const [year, month, day] = dateIso.split('-').map(Number)
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay()
  return weekday !== 0 && weekday !== 6
}

function resolveAuctionTradeDate(asOf: string, now = new Date()): string {
  const today = getCnTodayIso(now)
  if (!asOf || asOf < today) return isBusinessDateIso(today) ? today : nextBusinessDateIso(today)
  if (asOf > today || getCnNowMinutes(now) >= 15 * 60) return nextBusinessDateIso(asOf)
  return today
}

// 获取策略为占位功能, 暂时隐藏入口; 恢复时改回 true
const SHOW_STRATEGY_STORE = false

const HISTORY_PAGE_SIZE = 10
const HISTORY_EVENT_LABELS: Record<StrategyHistoryEvent['event_type'], string> = {
  selected: '策略选出',
  preselect: '盘后预选',
  auction_confirmed: '竞价确认',
  auction_rejected: '竞价淘汰',
  buy_signal: '买入信号',
  sell_signal: '卖出信号',
  pool_entry: '进入策略池',
  pool_exit: '移出策略池',
}

export function Screener() {
  const [assetType, setAssetType] = useState<'stock' | 'etf'>('stock')
  // 周期显示筛选: 全部 / 日线 / 分钟 — 只过滤卡片显示, 不影响池和执行;
  // 执行按每个策略自己声明的 timeframes 路由 (日线走盘后缓存, 分钟走本地分钟K分区)
  const [tfFilter, setTfFilter] = useState<'all' | '1d' | '1m'>('all')
  const [activeStrategy, setActiveStrategy] = useState<string | null>(null)
  const [result, setResult] = useState<ScreenerResult | null>(null)
  const [asOf, setAsOf] = useState<string>('')
  const [batchMsg, setBatchMsg] = useState<string>('')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')
  const closePreview = useCallback(() => { setPreviewSymbol(null); setPreviewName('') }, [])
  const [settingsStrategyId, setSettingsStrategyId] = useState<string | null>(null)
  const [showPoolDialog, setShowPoolDialog] = useState(false)
  const [showBuilder, setShowBuilder] = useState(false)
  const [builderMode, setBuilderMode] = useState<'create' | 'modify'>('create')
  const [showStore, setShowStore] = useState(false)
  const [showComposite, setShowComposite] = useState(false)
  const [showHistoryDialog, setShowHistoryDialog] = useState(false)
  const [historyPage, setHistoryPage] = useState(0)
  const { pool, addToPool, removeFromPool, reorderPool, prune } = useStrategyPool()
  const [cardSize, setCardSize] = useState<CardSize>(loadCardSize)
  // 日k蜡烛图显示开关（仅当 candle 列可见时才有意义；持久化）
  const [dailyKChartVisible, setDailyKChartVisible] = useState<boolean>(() => storage.screenerCandle.get(true))
  const toggleDailyKChart = useCallback(() => {
    setDailyKChartVisible(v => {
      const next = !v
      storage.screenerCandle.set(next)
      return next
    })
  }, [])
  // 分时图显示开关（仅当 intraday 列可见时才有意义；持久化）
  const [intradayChartVisible, setIntradayChartVisible] = useState<boolean>(() => storage.screenerIntraday.get(true))
  const toggleIntradayChart = useCallback(() => {
    setIntradayChartVisible(v => {
      const next = !v
      storage.screenerIntraday.set(next)
      return next
    })
  }, [])
  // 截断提示可关闭 (仅本次会话, 不持久化)
  const [intradayCapDismissed, setIntradayCapDismissed] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [showFilter, setShowFilter] = useState(false)
  const [filter, setFilter] = useState<ScreenerFilterType>(defaultFilter)
  const filterMap = useRef<Map<string, ScreenerFilterType>>(new Map())
  const runAllDateRef = useRef<string | null>(null)
  const qc = useQueryClient()

  // 结果列配置 — 默认内置列，异步合并后端/localStorage 偏好
  const [columns, setColumns] = useState<ColumnConfig[]>([...SCREENER_BUILTIN_COLUMNS])
  const [customizerOpen, setCustomizerOpen] = useState(false)
  const columnsLoaded = useRef(false)

  useEffect(() => {
    if (columnsLoaded.current) return
    columnsLoaded.current = true
    loadScreenerColumnConfig().then(setColumns)
  }, [])

  const handleColumnsChange = useCallback((next: ColumnConfig[]) => {
    setColumns(next)
    saveScreenerColumnConfig(next)
  }, [])

  const extColumnsParam = useMemo(() => buildExtColumnsParam(columns), [columns])

  // 各策略命中数 (进入页面自动跑)
  const [hitCounts, setHitCounts] = useState<Record<string, number>>({})
  // 各策略失效数 (今日曾命中 - 当前命中)
  const [expiredCounts, setExpiredCounts] = useState<Record<string, number>>({})
  // 各策略显示上限 (null = 全部)
  const [strategyLimits, setStrategyLimits] = useState<Record<string, number | null>>({})

  // 筛选条件变化时同步到 map（供切换策略时读取最新值）
  useEffect(() => {
    if (activeStrategy) filterMap.current.set(activeStrategy, filter)
  }, [filter, activeStrategy])

  // 切换策略时恢复该策略之前保存的筛选
  const handleStrategySwitch = useCallback((strategyId: string) => {
    setFilter(filterMap.current.get(strategyId) ?? { ...defaultFilter })
  }, [])

  // 对原始结果应用过滤 (memo: 否则每次渲染都对全部结果行过滤,
  // 且新数组身份会击穿下游 displayRows 的 memo)
  const filteredRows = useMemo(
    () => (result ? applyFilter(result.rows, filter) : []),
    [result, filter],
  )

  const { data: prefs } = usePreferences()
  const screenerAutoRun = prefs?.screener_auto_run ?? true
  const dataStatus = useDataStatus({ staleTime: 0 })

  // 统一列表: 不按周期过滤, 日线+分钟策略合并返回, 分钟策略带 timeframes 标识
  const strategies = useQuery({
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
  })

  // 激活策略自身的执行周期 (决定走缓存还是分钟实时跑)。
  // 在 queries 之前独立计算, 避免依赖下方 strategyMap 的定义顺序。
  const activeStrategyTimeframe = useMemo(() => {
    if (!activeStrategy) return '1d' as const
    const meta = (strategies.data?.presets ?? []).find(s => s.id === activeStrategy)
    return meta?.timeframes?.includes('1m') ? ('1m' as const) : ('1d' as const)
  }, [strategies.data, activeStrategy])

  // 卡片首屏只读取轻量摘要；明细在点击策略或“全部”时按需加载。
  // 摘要只覆盖日线缓存; 分钟策略命中数来自手动单跑。
  const summaryQuery = useQuery({
    queryKey: QK.screenerCachedSummary,
    queryFn: api.screenerCachedSummary,
    enabled: assetType === 'stock',
  })

  const fullCachedQuery = useQuery({
    queryKey: QK.screenerCached(asOf, extColumnsParam),
    queryFn: () => api.screenerCached(extColumnsParam || undefined),
    enabled: assetType === 'stock' && tfFilter !== '1m' && showAll,
  })

  const singleCachedQuery = useQuery({
    queryKey: QK.screenerCachedResult(activeStrategy ?? '', asOf, extColumnsParam),
    queryFn: () => api.screenerCachedResult(activeStrategy!, extColumnsParam || undefined),
    enabled: assetType === 'stock'
      && activeStrategyTimeframe === '1d'
      && !showAll
      && !!activeStrategy
      && summaryQuery.data?.results[activeStrategy]?.as_of === asOf,
  })

  // 默认日期 = enriched 最新日期（始终跟随最新）
  useEffect(() => {
    const latest = dataStatus.data?.enriched?.latest_date
    if (latest) setAsOf(latest)
  }, [dataStatus.data?.enriched?.latest_date])

  const strategyPresets = useMemo(
    () => (strategies.data?.presets ?? []).filter(s => s.asset_types.includes(assetType)),
    [strategies.data, assetType],
  )

  // 策略 ID → 名称映射
  const strategyIdToName = useMemo(() => {
    const map: Record<string, string> = {}
    for (const p of strategyPresets) {
      map[p.id] = p.name
    }
    return map
  }, [strategyPresets])

  // 策略 ID → 完整对象映射（避免每张卡片 find 遍历）
  const strategyMap = useMemo(() => {
    const map = new Map<string, ScreenerStrategy>()
    for (const p of strategyPresets) {
      map.set(p.id, p)
    }
    return map
  }, [strategyPresets])

  const allStrategyIds = useMemo(
    () => new Set((strategies.data?.presets ?? []).map(s => s.id)),
    [strategies.data],
  )
  const availableStrategyIds = useMemo(
    () => new Set(strategyPresets.map(s => s.id)),
    [strategyPresets],
  )
  const visiblePool = useMemo(() => pool.filter(id => availableStrategyIds.has(id)), [pool, availableStrategyIds])
  const auctionTradeDate = resolveAuctionTradeDate(asOf)
  const auctionStrategyIdsKey = useMemo(() => visiblePool.join(','), [visiblePool])
  const isLatestStockDate = assetType === 'stock'
    && !!asOf
    && asOf === dataStatus.data?.enriched?.latest_date
  const preselectQuery = useQuery({
    queryKey: QK.screenerPreselect(asOf, auctionTradeDate, auctionStrategyIdsKey, 5, extColumnsParam || undefined),
    queryFn: () => api.screenerPreselect(
      asOf,
      auctionTradeDate,
      visiblePool,
      5,
      extColumnsParam || undefined,
      assetType,
    ),
    enabled: isLatestStockDate && visiblePool.length > 0,
    staleTime: 60_000,
  })
  const auctionConfirmationQuery = useQuery({
    queryKey: QK.screenerAuctionConfirmation(asOf, auctionTradeDate, auctionStrategyIdsKey, extColumnsParam || undefined),
    queryFn: () => api.screenerAuctionConfirmation(
      asOf,
      auctionTradeDate,
      visiblePool,
      extColumnsParam || undefined,
      assetType,
    ),
    enabled: isLatestStockDate && visiblePool.length > 0,
    staleTime: 0,
    refetchInterval: () => {
      const nowMinutes = getCnNowMinutes()
      if (nowMinutes < AUCTION_GATE_MINUTES) return 5_000
      if (nowMinutes < AUCTION_CLOSE_MINUTES) return 3_000
      if (nowMinutes < AUCTION_CONFIRM_GRACE_MINUTES) return 10_000
      return false
    },
    refetchIntervalInBackground: true,
  })
  const auctionNowMinutes = getCnNowMinutes()
  const auctionIsToday = auctionTradeDate === getCnTodayIso()
  const auctionDynamicQuery = useQuery({
    queryKey: QK.screenerAuctionReplay(asOf, auctionTradeDate, auctionStrategyIdsKey, 'live', 'recompute'),
    queryFn: () => api.auctionReplay({
      asOf,
      tradeDate: auctionTradeDate,
      strategyIds: visiblePool,
      asOfTs: Date.now(),
      mode: 'recompute',
      includeFrames: false,
      includeCandidates: true,
      maxFrames: 1,
      assetType,
    }),
    enabled: isLatestStockDate
      && visiblePool.length > 0
      && auctionIsToday
      && auctionNowMinutes >= AUCTION_PREWARM_MINUTES,
    staleTime: 0,
    refetchInterval: () => {
      const nowMinutes = getCnNowMinutes()
      if (nowMinutes < AUCTION_PREWARM_MINUTES) return false
      if (nowMinutes < AUCTION_GATE_MINUTES) return 5_000
      if (nowMinutes < AUCTION_CLOSE_MINUTES) return 1_000
      if (nowMinutes < AUCTION_CONFIRM_GRACE_MINUTES) return 5_000
      return false
    },
    refetchIntervalInBackground: true,
  })
  const auctionDynamicPayload = auctionDynamicQuery.data
  const auctionDynamicFrame = auctionDynamicPayload?.frame ?? auctionDynamicPayload?.final_frame ?? null
  const auctionDynamicActive = (
    assetType === 'stock'
    && auctionDynamicPayload?.mode === 'auction_dynamic'
    && auctionDynamicPayload?.as_of === asOf
    && auctionDynamicPayload?.trade_date === auctionTradeDate
    && auctionNowMinutes >= AUCTION_GATE_MINUTES
    && !!auctionDynamicFrame
  )
  const auctionDynamicDisplayResults = useMemo<Record<string, any> | null>(() => {
    if (!auctionDynamicActive || !auctionDynamicFrame) return null
    const finalized = auctionDynamicPayload?.status === 'ready'
    const entries = Object.entries(auctionDynamicFrame.results)
      .filter(([, item]) => item.as_of === asOf)
      .map(([sid, item]) => {
        const rows = finalized
          ? item.rows ?? []
          : item.candidates ?? item.dual_rows ?? item.rows ?? []
        return [sid, {
          ...item,
          as_of: asOf,
          rows,
          total: finalized
            ? item.final_total ?? item.confirmed_total ?? item.total ?? rows.length
            : item.candidate_total ?? item.base_total ?? item.total ?? rows.length,
        }]
      })
    return Object.fromEntries(entries)
  }, [auctionDynamicActive, auctionDynamicFrame, auctionDynamicPayload?.status, asOf])
  const auctionDynamicHasResults = !!auctionDynamicDisplayResults
    && Object.values(auctionDynamicDisplayResults).some(item => item.total > 0)
  const auctionDynamicFinalReady = auctionDynamicActive && auctionDynamicPayload?.status === 'ready'

  // 卡片显示: 按周期筛选 (all=全部, 1d=仅日线, 1m=仅分钟); 未声明 timeframes 视为日线
  const displayPool = useMemo(() => visiblePool.filter(id => {
    if (tfFilter === 'all') return true
    const isMinute = strategyMap.get(id)?.timeframes?.includes('1m') ?? false
    return tfFilter === '1m' ? isMinute : !isMinute
  }), [visiblePool, strategyMap, tfFilter])

  // runAll/盘后缓存只覆盖日线策略; 池中分钟策略由手动单跑实时计算
  const dailyPoolIds = useMemo(
    () => visiblePool.filter(id => !(strategyMap.get(id)?.timeframes?.includes('1m') ?? false)),
    [visiblePool, strategyMap],
  )

  // 策略列表加载后,自动清除池中失效的自定义策略(如本地开发残留的、
  // 当前后端已不存在的策略 ID),避免"策略池"对话框持续显示失效项。
  // 关键: 仅当本次拉取成功且返回非空列表时才 prune。
  // 拉取中/失败/返回空(如引擎 reload 瞬时把某策略跳过)时一律不碰池,
  // 否则会把用户池里仍有效的 ID 永久清空并写入 localStorage,导致卡片全没。
  // 日线/分钟池按周期隔离, 各自用自身周期的列表清理, 互不影响。
  useEffect(() => {
    if (strategies.isError) return        // 拉取失败: 不 prune
    if (!strategies.isSuccess) return     // 加载中: 不 prune
    if (allStrategyIds.size === 0) return  // 空列表: 不 prune
    prune(allStrategyIds)
  }, [allStrategyIds, prune, strategies.isError, strategies.isSuccess])

  // 策略文件加载失败时提示用户(避免"策略静默消失"被误判为正常)
  const loadErrors = strategies.data?.load_errors ?? []
  useEffect(() => {
    for (const e of loadErrors) {
      toast(`策略「${e.file}」加载失败：${e.error}`, 'error')
    }
  }, [loadErrors])

  // 进入页面自动跑策略池中的策略，获取命中数 (仅日线策略; 分钟策略手动单跑)
  const runAll = useMutation({
    mutationFn: ({ date, strategyIds }: { date?: string; strategyIds?: string[] } = {}) =>
      api.screenerRunAll(
        date,
        strategyIds ?? dailyPoolIds,
        extColumnsParam || undefined,
        assetType,
      ),
    onSuccess: (data) => {
      if (data.as_of) setAsOf(data.as_of)
      const counts: Record<string, number> = {}
      for (const [id, item] of Object.entries(data.results)) {
        counts[id] = item.total
      }
      setHitCounts(prev => ({ ...prev, ...counts }))
      qc.invalidateQueries({ queryKey: ['screener-cached'] })
      qc.invalidateQueries({ queryKey: ['screener-auction-confirmation'] })
      qc.invalidateQueries({ queryKey: ['screener-auction-replay'] })
      qc.invalidateQueries({ queryKey: ['screener-preselect'] })
    },
  })

  const missingStrategyIds = useMemo(
    () => dailyPoolIds.filter(id => summaryQuery.data?.results[id]?.as_of !== asOf),
    [dailyPoolIds, summaryQuery.data, asOf],
  )
  const cacheCoversPool = dailyPoolIds.length > 0 && missingStrategyIds.length === 0

  // 防止 reload / auto-run / StrictMode 叠出并发 run_all（后端 Numba 会崩溃）
  // 用 ref 同步门闩，避免同一渲染周期内 isPending 尚未更新导致重复触发
  const runAllPendingRef = useRef(false)
  const requestRunAll = useCallback((
    vars: { date?: string; strategyIds?: string[] } = {},
    options?: Parameters<typeof runAll.mutate>[1],
  ) => {
    if (runAllPendingRef.current || runAll.isPending) return
    runAllPendingRef.current = true
    runAll.mutate(vars, {
      ...options,
      onSettled: (...args) => {
        runAllPendingRef.current = false
        options?.onSettled?.(...args)
      },
    })
  }, [runAll])

  // 摘要只同步当前日期的卡片数量，避免旧日期缓存短暂显示成当前结果。
  useEffect(() => {
    if (!summaryQuery.data || !asOf) return
    const counts: Record<string, number> = {}
    const expired: Record<string, number> = {}
    for (const [id, r] of Object.entries(summaryQuery.data.results)) {
      if (r.as_of !== asOf) continue
      counts[id] = r.total
      const everCount = summaryQuery.data.today_ever_counts[id] ?? r.total
      const expiredCount = Math.max(everCount - r.total, 0)
      if (expiredCount > 0) expired[id] = expiredCount
    }
    setHitCounts(counts)
    setExpiredCounts(expired)
  }, [summaryQuery.data, asOf])

  // 当前单策略缓存更新后同步明细；参数保存的强制重算结果仍由 run 直接覆盖。
  useEffect(() => {
    const cached = singleCachedQuery.data?.result
    if (!cached || showAll || cached.strategy !== activeStrategy || cached.as_of !== asOf) return
    setResult(cached)
    if (activeStrategy) {
      setHitCounts(prev => ({ ...prev, [activeStrategy]: cached.total }))
    }
  }, [singleCachedQuery.data, showAll, activeStrategy, asOf])

  useEffect(() => {
    if (showAll || !activeStrategy) return
    const confirmed = auctionConfirmationQuery.data
    if (!confirmed || confirmed.gate_status !== 'confirmed' || confirmed.as_of !== asOf || confirmed.trade_date !== auctionTradeDate) return
    const next = confirmed.results[activeStrategy]
    if (!next) return
    setResult((prev) => {
      const base = prev && prev.strategy === activeStrategy && prev.as_of === asOf
        ? prev
        : singleCachedQuery.data?.result
      if (!base) {
        return {
          as_of: confirmed.as_of ?? asOf ?? '',
          strategy: activeStrategy,
          rows: next.rows,
          total: next.total,
          elapsed_ms: 0,
        }
      }
      if (base.total === next.total && base.rows === next.rows) return base
      return {
        ...base,
        rows: next.rows,
        total: next.total,
      }
    })
  }, [auctionConfirmationQuery.data, auctionTradeDate, showAll, activeStrategy, asOf, singleCachedQuery.data?.result])

  useEffect(() => {
    if (showAll || !activeStrategy || !auctionDynamicActive || !auctionDynamicFrame) return
    if (!auctionDynamicHasResults && !auctionDynamicFinalReady) return
    const confirmed = auctionConfirmationQuery.data
    if (confirmed?.gate_status === 'confirmed' && confirmed.as_of === asOf && confirmed.trade_date === auctionTradeDate) return
    const next = auctionDynamicFrame.results[activeStrategy]
    if (!next) return
    const rows = next.candidates ?? next.dual_rows ?? next.rows ?? []
    setResult((prev) => {
      const base = prev && prev.strategy === activeStrategy && prev.as_of === asOf
        ? prev
        : singleCachedQuery.data?.result
      return {
        ...(base ?? {
          as_of: asOf,
          strategy: activeStrategy,
          elapsed_ms: 0,
        }),
        rows,
        total: next.candidate_total ?? next.base_total ?? next.total ?? rows.length,
        elapsed_ms: next.elapsed_ms ?? auctionDynamicFrame.elapsed_ms ?? base?.elapsed_ms ?? 0,
      }
    })
  }, [
    auctionDynamicActive,
    auctionDynamicHasResults,
    auctionDynamicFinalReady,
    auctionDynamicFrame,
    auctionConfirmationQuery.data,
    auctionTradeDate,
    showAll,
    activeStrategy,
    asOf,
    singleCachedQuery.data?.result,
  ])

  const effectiveResults = useMemo(() => {
    if (fullCachedQuery.data?.as_of !== asOf) return null
    const entries = Object.entries(fullCachedQuery.data.results)
      .filter(([, item]) => item.as_of === asOf)
    return Object.fromEntries(entries)
  }, [fullCachedQuery.data, asOf])

  const auctionConfirmationPayload = auctionConfirmationQuery.data
  const auctionConfirmationReady = auctionConfirmationPayload?.gate_status === 'confirmed'
  const auctionConfirmationActive = (
    assetType === 'stock'
    && auctionConfirmationReady
    && auctionConfirmationPayload?.as_of === asOf
    && auctionConfirmationPayload?.trade_date === auctionTradeDate
  )
  const auctionDynamicTotals = useMemo(() => {
    if (!auctionDynamicActive || !auctionDynamicFrame) return { final: 0, candidates: 0 }
    return Object.values(auctionDynamicFrame.results).reduce((acc, item) => {
      acc.final += item.final_total ?? item.confirmed_total ?? item.total ?? 0
      acc.candidates += item.candidate_total ?? item.base_total ?? item.total ?? 0
      return acc
    }, { final: 0, candidates: 0 })
  }, [auctionDynamicActive, auctionDynamicFrame])
  const strictPoolTotal = useMemo(() => {
    if (!cacheCoversPool) return null
    return visiblePool.reduce((sum, id) => sum + (summaryQuery.data?.results[id]?.total ?? 0), 0)
  }, [cacheCoversPool, visiblePool, summaryQuery.data])
  const preselectPayload = preselectQuery.data
  const preselectResults = useMemo<Record<string, any> | null>(() => {
    if (
      !preselectPayload
      || preselectPayload.mode !== 'preselect'
      || preselectPayload.as_of !== asOf
      || preselectPayload.trade_date !== auctionTradeDate
    ) return null
    const entries = Object.entries(preselectPayload.results)
      .filter(([, item]) => item.as_of === asOf)
      .map(([sid, item]) => [sid, {
        ...item,
        rows: item.rows ?? [],
        total: item.preselect_total ?? item.total ?? item.rows?.length ?? 0,
      }])
    return Object.fromEntries(entries)
  }, [preselectPayload, asOf, auctionTradeDate])
  const preselectTotal = useMemo(() => {
    if (!preselectResults) return 0
    return Object.values(preselectResults).reduce((sum, item) => sum + (item.total ?? 0), 0)
  }, [preselectResults])
  const activePreselectRows = useMemo(() => {
    if (showAll || !activeStrategy || !preselectResults) return []
    return preselectResults[activeStrategy]?.rows ?? []
  }, [showAll, activeStrategy, preselectResults])
  const preselectActive = (
    assetType === 'stock'
    && strictPoolTotal === 0
    && !auctionConfirmationActive
    && !!preselectResults
  )
  const displayMode: 'confirmed' | 'dynamic' | 'preselect' | 'cached' = auctionConfirmationActive
    ? 'confirmed'
    : auctionDynamicFinalReady || (auctionDynamicActive && auctionDynamicHasResults)
      ? 'dynamic'
      : preselectActive
        ? 'preselect'
        : 'cached'
  const displayHitCounts = useMemo(() => {
    if (displayMode === 'confirmed' && auctionConfirmationPayload) {
      const next = { ...hitCounts }
      for (const [sid, item] of Object.entries(auctionConfirmationPayload.results)) {
        if (item.as_of === asOf) next[sid] = item.total
      }
      return next
    }
    if (displayMode === 'dynamic' && auctionDynamicDisplayResults) {
      const next = { ...hitCounts }
      for (const [sid, item] of Object.entries(auctionDynamicDisplayResults)) {
        if (item.as_of === asOf) next[sid] = item.total
      }
      return next
    }
    if (displayMode !== 'preselect' || !preselectResults) return hitCounts
    const next = { ...hitCounts }
    for (const [sid, item] of Object.entries(preselectResults)) {
      if (item.as_of === asOf) next[sid] = item.total
    }
    return next
  }, [displayMode, auctionConfirmationPayload, auctionDynamicDisplayResults, preselectResults, hitCounts, asOf])
  const displayAllResults = useMemo<Record<string, any> | null>(() => {
    if (!showAll) return effectiveResults
    if (displayMode === 'confirmed' && auctionConfirmationPayload) {
      const entries = Object.entries(auctionConfirmationPayload.results)
        .filter(([, item]) => item.as_of === asOf)
      return Object.fromEntries(entries)
    }
    if (displayMode === 'dynamic' && auctionDynamicDisplayResults) return auctionDynamicDisplayResults
    if (displayMode === 'preselect' && preselectResults) return preselectResults
    if (!auctionConfirmationActive || !auctionConfirmationPayload) return effectiveResults
    const entries = Object.entries(auctionConfirmationPayload.results)
      .filter(([, item]) => item.as_of === asOf)
    return Object.fromEntries(entries)
  }, [showAll, effectiveResults, displayMode, auctionConfirmationPayload, auctionDynamicDisplayResults, preselectResults, auctionConfirmationActive, asOf])

  useEffect(() => {
    if (showAll || !activeStrategy || displayMode !== 'preselect' || !preselectResults) return
    const next = preselectResults[activeStrategy]
    if (!next) return
    setResult((prev) => {
      const base = prev && prev.strategy === activeStrategy && prev.as_of === asOf
        ? prev
        : singleCachedQuery.data?.result
      return {
        ...(base ?? {
          as_of: asOf,
          strategy: activeStrategy,
          elapsed_ms: 0,
        }),
        rows: next.rows ?? [],
        total: next.total ?? next.preselect_total ?? next.rows?.length ?? 0,
        elapsed_ms: base?.elapsed_ms ?? 0,
      }
    })
  }, [showAll, activeStrategy, displayMode, preselectResults, asOf, singleCachedQuery.data?.result])

  // symbol → 所属策略列表。单策略接口同时返回轻量归属映射，保留策略列原有展示。
  const symbolStrategyMap = useMemo(() => {
    const map = new Map<string, string[]>()
    if (showAll) {
      for (const [sid, r] of Object.entries(displayAllResults ?? {})) {
        for (const row of r.rows) {
          const arr = map.get(row.symbol)
          if (arr) arr.push(sid)
          else map.set(row.symbol, [sid])
        }
      }
      return map
    }
    for (const [symbol, ids] of Object.entries(singleCachedQuery.data?.strategy_ids_by_symbol ?? {})) {
      map.set(symbol, ids)
    }
    if (activeStrategy && result) {
      for (const row of result.rows) {
        if (!map.has(row.symbol)) map.set(row.symbol, [activeStrategy])
      }
    }
    return map
  }, [showAll, displayAllResults, singleCachedQuery.data, activeStrategy, result])

  // "全部" 模式: 合并所有策略的去重个股
  const allRows = useMemo(() => {
    if (!displayAllResults) return []
    const seen = new Set<string>()
    const merged: any[] = []
    for (const r of Object.values(displayAllResults)) {
      for (const row of r.rows) {
        if (!seen.has(row.symbol)) {
          seen.add(row.symbol)
          merged.push(row)
        }
      }
    }
    return merged
  }, [displayAllResults])

  // 计算当前策略的失效行: 今日曾命中但当前已不命中。
  const expiredRows = useMemo(() => {
    if (displayMode !== 'cached') return []
    const everRows = singleCachedQuery.data?.today_ever_rows
    if (!everRows || !result || result.as_of !== asOf) return []
    const currentSymbols = new Set(result.rows.map((row: any) => row.symbol))
    return Object.entries(everRows)
      .filter(([symbol]) => !currentSymbols.has(symbol))
      .map(([, row]) => ({ ...row, _expired: true }))
  }, [displayMode, singleCachedQuery.data, result, asOf])

  // 表头排序（受控）：用户点击列则按该列；未点时下方按评分默认降序
  const { sort, toggle, sortRows } = useTableSort()

  // 当前显示的行数据 (全部模式 或 单策略模式) + 失效行
  const displayRows = useMemo(() => {
    let rows = showAll
      ? applyFilter(allRows, filter)
      : filteredRows
    // 排序：用户点了表头则按该列，否则默认评分降序
    rows = sort
      ? sortRows(rows, columns)
      : [...rows].sort((a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity))
    const limit = !showAll && activeStrategy
      ? strategyLimits[activeStrategy] ?? null
      : null
    const mainRows = limit != null ? rows.slice(0, limit) : rows

    // 追加当前策略的失效行 (灰色)
    if (!showAll && activeStrategy) {
      if (expiredRows.length > 0) {
        return [...mainRows, ...expiredRows]
      }
    }
    return mainRows
  }, [showAll, allRows, filteredRows, filter, activeStrategy, strategyLimits, expiredRows, sort, sortRows, columns])

  // 日k列是否启用 → 决定是否加载批量 kline 数据
  const candleColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle' && c.visible),
    [columns],
  )
  const candleColumnEnabled = !!candleColumn
  // 日k天数（来自列配置，已钳制边界）
  const candleDays = useMemo(() => resolveCandleConfig(candleColumn?.candleConfig).days, [candleColumn])
  // 真正请求/渲染蜡烛图：列可见 且 眼睛开关开启
  const dailyKVisible = candleColumnEnabled && dailyKChartVisible

  // 批量日k数据 (仅当蜡烛图可见时加载，省请求)
  const dailyKSymbols = useMemo(
    () => [...new Set(displayRows.map((r: any) => r.symbol as string))].sort(),
    [displayRows],
  )
  const resultSymbolsKey = dailyKSymbols.join(',')
  const klineBatch = useQuery({
    queryKey: QK.screenerKlineBatch(`${resultSymbolsKey}|${candleDays}`),
    queryFn: () => api.klineDailyBatch(dailyKSymbols, candleDays),
    enabled: dailyKVisible && dailyKSymbols.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: previousData => previousData,
  })
  const klineData = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}

  // 分时列是否启用 → 决定是否加载批量分时数据 (需 kline.minute.batch 能力)
  const intradayColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible),
    [columns],
  )
  // 分时图依赖分钟K批量数据 (kline.minute.batch), 无数据时开了列也不拉
  const caps = useCapabilities()
  const hasMinuteBatch = !!caps.data?.capabilities?.['kline.minute.batch']
  const intradayVisible = !!intradayColumn && hasMinuteBatch && intradayChartVisible

  // 分时数据加载策略 (与自选页一致, 简洁优先):
  //  - 全量加载当前列表 symbol, 但按数据源 batch 上限截断,
  //    超出时只取前 batch 只并提示用户, 避免一次性发太多请求打爆 rpm 配额
  //  - 刷新: minute_intraday_refresh 偏好开启时按用户设定间隔轮询; 否则仅首次加载,
  //    用户可点表头刷新按钮手动更新
  const minuteBatchCap = caps.data?.capabilities?.['kline.minute.batch']?.batch ?? 100
  const quoteStatus = useQuoteStatus()
  const realtimeRunning = quoteStatus.data?.running ?? false
  const intradayRefreshEnabled = prefs?.minute_intraday_refresh ?? false
  const intradayRefreshInterval = prefs?.minute_intraday_refresh_interval ?? 6

  const allIntradaySymbols = useMemo(
    () => displayRows.map((r: any) => r.symbol),
    [displayRows],
  )
  const intradayTruncated = intradayVisible && allIntradaySymbols.length > minuteBatchCap
  // 截断到 batch 上限, 一次请求 = 一次数据源调用
  const intradaySymbols = useMemo(
    () => intradayTruncated ? allIntradaySymbols.slice(0, minuteBatchCap) : allIntradaySymbols,
    [allIntradaySymbols, intradayTruncated, minuteBatchCap],
  )
  const intradayRequestSymbols = useMemo(
    () => [...new Set(intradaySymbols)].sort(),
    [intradaySymbols],
  )
  const intradaySymbolsKey = intradayRequestSymbols.join(',')

  const minuteBatch = useQuery({
    queryKey: QK.minuteBatch(intradaySymbolsKey),
    queryFn: () => api.klineMinuteBatch(intradayRequestSymbols),
    enabled: intradayVisible && intradayRequestSymbols.length > 0,
    staleTime: 10_000,
    placeholderData: previousData => previousData,
    // 仅当开启分时刷新偏好 且 盘中实时行情运行时 才轮询 (省 rpm)
    refetchInterval: (intradayRefreshEnabled && realtimeRunning) ? intradayRefreshInterval * 1000 : false,
  })
  const minuteData = intradayVisible ? (minuteBatch.data?.data ?? {}) : {}

  // asOf 确定后 + 策略列表就绪 + 策略池非空 → 自动跑一次 (受系统设置开关控制)
  // 缓存命中时秒加载; 未命中时, 仅当 screener_auto_run 开启才自动触发 runAll
  useEffect(() => {
    // ETF 模式无股票盘后缓存/ runAll, 单策略走实时单跑, 不触发 runAll
    // 分钟筛选视图下不跑日线缓存 (切回 全部/日线 视图时本 effect 会重新评估)
    if (assetType !== 'stock' || tfFilter === '1m') return
    if (!asOf || strategyPresets.length === 0 || !summaryQuery.isSuccess || runAll.isPending || dailyPoolIds.length === 0) return
    const runKey = `${asOf}|${dailyPoolIds.join(',')}`
    if (runAllDateRef.current === runKey) return
    // 缓存已覆盖当前策略池 → 秒加载, 不触发 runAll
    if (cacheCoversPool) {
      runAllDateRef.current = runKey
      return
    }
    // 未覆盖: 受系统开关控制
    if (!screenerAutoRun) return
    runAllDateRef.current = runKey
    requestRunAll({ date: asOf, strategyIds: missingStrategyIds })
  }, [asOf, strategyPresets.length, summaryQuery.isSuccess, dailyPoolIds, cacheCoversPool, missingStrategyIds, screenerAutoRun, assetType, tfFilter, runAll.isPending, requestRunAll])

  // 执行周期由策略自身声明决定: 日线走盘后缓存/单跑, 分钟走本地分钟K分区实时跑
  const run = useMutation({
    mutationFn: ({ id, date, timeframe: tf }: { id: string; date: string; timeframe: '1d' | '1m' }) =>
      api.screenerRunPreset(id, undefined, date || undefined, extColumnsParam || undefined, assetType, tf),
    onSuccess: (data, vars) => {
      setResult(data)
      // 同步更新卡片上的命中数
      setHitCounts(prev => ({ ...prev, [vars.id]: data.total }))
      // 单策略重跑后刷新摘要和当前按需明细，避免参数保存后回退到旧缓存。
      qc.invalidateQueries({ queryKey: ['screener-cached'] })
      qc.invalidateQueries({ queryKey: ['screener-auction-confirmation'] })
      qc.invalidateQueries({ queryKey: ['screener-auction-replay'] })
      qc.invalidateQueries({ queryKey: ['screener-preselect'] })
    },
  })

  const handleRun = (s: ScreenerStrategy) => {
    handleStrategySwitch(s.id)
    setActiveStrategy(s.id)
    setShowAll(false)
    if (result?.strategy !== s.id || result.as_of !== asOf) setResult(null)
    const tf = s.timeframes?.includes('1m') ? '1m' as const : '1d' as const
    // ETF 模式无股票盘后缓存、分钟策略走本地分钟分区 → 始终实时单跑。
    // 传空日期让后端用自身的最新交易日 (ETF 与分钟分区跟股票 enriched 可能不同日)。
    if (assetType !== 'stock' || tf === '1m') {
      run.mutate({ id: s.id, date: '', timeframe: tf })
      return
    }
    // 摘要命中时由 singleCachedQuery 按需加载明细；缺失时才单独计算。
    if (summaryQuery.data?.results[s.id]?.as_of === asOf || runAll.isPending) return
    run.mutate({ id: s.id, date: asOf, timeframe: tf })
  }

  // 日期变化交给统一 effect 计算一次，避免这里与 effect 重复请求。
  const handleDateChange = (newDate: string) => {
    setAsOf(newDate)
    runAllDateRef.current = null
    setResult(null)
  }

  const minDate = dataStatus.data?.enriched?.earliest_date ?? ''
  const maxDate = dataStatus.data?.enriched?.latest_date ?? ''

  const batchAdd = useWatchlistBatchAdd()

  // 自选股列表 (用于判断是否在自选中)
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
  })
  const watchlistSet = useMemo(() => {
    const symbols = watchlist.data?.symbols ?? []
    return new Set(symbols.map((s: any) => s.symbol))
  }, [watchlist.data])

  // 策略结果页的用户买入标记: 以 strategy + signal_date 隔离, 避免同一股票
  // 在不同策略或不同交易日的标记互相覆盖。
  const purchaseMarksQuery = useQuery({
    queryKey: QK.strategyPurchaseMarks,
    queryFn: api.strategyPurchaseMarks,
  })
  const purchaseMarks = useMemo(() => {
    const map = new Map<string, StrategyPurchaseMark>()
    for (const mark of purchaseMarksQuery.data?.marks ?? []) {
      map.set(`${mark.strategy_id}::${mark.symbol.toUpperCase()}::${mark.signal_date}`, mark)
    }
    return map
  }, [purchaseMarksQuery.data])

  // 策略候选生命周期: 即使今日动态结果归零,仍展示此前候选的竞价确认/淘汰节点。
  const strategyHistoryQuery = useQuery({
    queryKey: QK.strategyHistory(activeStrategy ?? '', 180),
    queryFn: async () => {
      await api.strategyHistoryBackfill(activeStrategy ? [activeStrategy] : undefined)
      return api.strategyHistory({
        strategyId: activeStrategy ?? undefined,
        days: 180,
        limit: 1000,
      })
    },
    enabled: assetType === 'stock' && !!activeStrategy,
    staleTime: 10_000,
  })
  const strategyHistoryEvents = strategyHistoryQuery.data?.events ?? []
  const recommendationHistoryEvents = useMemo(() => {
    const recommendationTypes = new Set<StrategyHistoryEvent['event_type']>([
      'selected',
      'preselect',
      'auction_confirmed',
      'auction_rejected',
      'buy_signal',
    ])
    const monitorTypes = new Set<StrategyHistoryEvent['event_type']>([
      'sell_signal',
      'pool_entry',
      'pool_exit',
    ])
    const recommendationMarks = new Map<string, { signalDate: string; ts: number }[]>()
    for (const event of strategyHistoryEvents) {
      if (!recommendationTypes.has(event.event_type)) continue
      const symbol = event.symbol.toUpperCase()
      recommendationMarks.set(symbol, [
        ...(recommendationMarks.get(symbol) ?? []),
        { signalDate: event.signal_date, ts: event.ts },
      ])
    }
    return strategyHistoryEvents.filter(event =>
      recommendationTypes.has(event.event_type)
      || (monitorTypes.has(event.event_type)
        && (recommendationMarks.get(event.symbol.toUpperCase()) ?? []).some(
          mark => mark.signalDate < event.signal_date
            || (mark.signalDate === event.signal_date && mark.ts <= event.ts),
        )),
    )
  }, [strategyHistoryEvents])
  const intradayBuyCount = useMemo(
    () => recommendationHistoryEvents.filter(event =>
      event.event_type === 'buy_signal' && event.signal_date === asOf,
    ).length,
    [recommendationHistoryEvents, asOf],
  )
  const historyPageCount = Math.max(1, Math.ceil(recommendationHistoryEvents.length / HISTORY_PAGE_SIZE))
  const historyPageEvents = recommendationHistoryEvents.slice(
    historyPage * HISTORY_PAGE_SIZE,
    (historyPage + 1) * HISTORY_PAGE_SIZE,
  )

  useEffect(() => {
    setHistoryPage(0)
  }, [activeStrategy, strategyHistoryQuery.data])

  useEffect(() => {
    if (historyPage >= historyPageCount) setHistoryPage(Math.max(historyPageCount - 1, 0))
  }, [historyPage, historyPageCount])

  const togglePurchaseMark = useMutation({
    mutationFn: ({
      strategyId,
      symbol,
      signalDate,
      signalPrice,
      signalScore,
      signalChangePct,
      marked,
    }: {
      strategyId: string
      symbol: string
      signalDate: string
      signalPrice: number | null
      signalScore: number | null
      signalChangePct: number | null
      marked: boolean
    }) => marked
      ? api.strategyPurchaseMarkDelete(strategyId, symbol, signalDate)
      : api.strategyPurchaseMarkSave({
          strategy_id: strategyId,
          strategy_name: strategyIdToName[strategyId] ?? strategyId,
          symbol,
          signal_date: signalDate,
          signal_price: signalPrice,
          signal_score: signalScore,
          signal_change_pct: signalChangePct,
          note: '策略页用户手动标记已买入',
        }),
    onSuccess: (_, variables) => {
      qc.invalidateQueries({ queryKey: QK.strategyPurchaseMarks })
      toast(variables.marked ? '已取消买入标记' : '已记录用户买入标记', 'success')
    },
  })

  // 单只股票加入/移出自选
  const toggleWatchlist = useMutation({
    mutationFn: ({
      symbol,
      action,
      groupId,
    }: {
      symbol: string
      action: 'add' | 'remove'
      groupId?: string | null
    }) => action === 'remove'
      ? api.watchlistRemove(symbol)
      : api.watchlistAdd(symbol, '', groupId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  // 重新运行策略：重载策略文件 + 重跑全部策略，刷新符合条件的个股
  const reloadStrategies = useMutation({
    mutationFn: api.strategyReload,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['screener-strategies'] })
      qc.invalidateQueries({ queryKey: ['screener-auction-confirmation'] })
      qc.invalidateQueries({ queryKey: ['screener-auction-replay'] })
      qc.invalidateQueries({ queryKey: ['screener-preselect'] })
      if (asOf) requestRunAll({ date: asOf })
    },
  })

  // 策略监控: 查询规则, 建立 strategyId → ruleId 映射 (只看 type=strategy 且 enabled)
  const monitorRules = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList })
  const strategyMonitorMap = useMemo(() => {
    const m = new Map<string, string>()
    for (const r of monitorRules.data?.rules ?? []) {
      if (r.type === 'strategy' && r.enabled && r.strategy_id) {
        m.set(r.strategy_id, r.id)
      }
    }
    return m
  }, [monitorRules.data])

  const toggleStrategyMonitor = (strategyId: string, strategyName: string) => {
    const existingRuleId = strategyMonitorMap.get(strategyId)
    if (existingRuleId) {
      // 已监控 → 删除规则
      api.monitorRuleDelete(existingRuleId).then(() =>
        qc.invalidateQueries({ queryKey: QK.monitorRules }),
      )
    } else {
      // 未监控 → 直接创建 type=strategy 规则
      api.monitorRuleSave({
        id: genRuleId(),
        name: `策略监控 · ${strategyName}`,
        enabled: true,
        type: 'strategy',
        scope: 'all',
        symbols: [],
        sector: null,
        strategy_id: strategyId,
        direction: 'entry',
        notify_events: [...DEFAULT_STRATEGY_NOTIFY_EVENTS],
        conditions: [],
        logic: 'or',
        cooldown_seconds: 3600,
        severity: 'info',
        message: '',
      }).then(() => qc.invalidateQueries({ queryKey: QK.monitorRules }))
    }
  }

  const handleBatchAdd = (groupId: string | null) => {
    if (!displayRows.length) return
    const symbols = displayRows.map((r: any) => r.symbol)
    batchAdd.mutate({ symbols, groupId }, {
      onSuccess: (data) => {
        setBatchMsg(`已添加 ${data.added} 只到自选`)
        setTimeout(() => setBatchMsg(''), 3000)
      },
      onError: () => {
        setBatchMsg('添加失败')
        setTimeout(() => setBatchMsg(''), 3000)
      },
    })
  }

  const resultPanelVisible = showAll
    ? allRows.length > 0 || displayMode !== 'cached'
    : !!result
  const resultLabel = displayMode === 'preselect'
    ? '盘后预选'
    : displayMode === 'confirmed'
      ? '竞价确认'
      : displayMode === 'dynamic'
        ? '动态竞价'
        : '正式命中'
  const auctionWaitingHint = `已生成盘后预选，等待 ${auctionTradeDate} 09:25 竞价确认`

  return (
    <>
      <PageHeader
        title="策略"
        subtitle="基于本地 enriched 表 · 毫秒级 SQL"
        right={
          <div className="flex items-center gap-2">
            {/* 资产类型切换: 股票 / ETF (分钟策略 asset_types 仅股票, ETF 列表自然不含) */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['stock', 'etf'] as const).map(t => (
                <button
                  key={t}
                  onClick={() => { setAssetType(t); setActiveStrategy(null); setResult(null); setShowAll(false) }}
                  className={`h-full px-2.5 text-xs font-medium transition-colors
                    cursor-pointer ${assetType === t
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {t === 'stock' ? '股票' : 'ETF'}
                </button>
              ))}
            </div>
            {/* 周期筛选: 全部 / 日线 / 分钟 — 只过滤卡片显示, 不影响池与执行路由 */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['all', '1d', '1m'] as const).map(tf => (
                <button
                  key={tf}
                  onClick={() => {
                    if (tfFilter === tf) return
                    setTfFilter(tf)
                    setActiveStrategy(null); setResult(null); setShowAll(false)
                  }}
                  className={`h-full px-2.5 text-xs font-medium transition-colors cursor-pointer
                    ${tfFilter === tf
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {tf === 'all' ? '全部' : tf === '1d' ? '日线' : '分钟'}
                </button>
              ))}
            </div>
            {/* 重新运行策略：重载策略文件并重跑全部策略，更新命中个股 */}
            <button
              onClick={() => reloadStrategies.mutate()}
              disabled={reloadStrategies.isPending}
              title="重新加载策略并运行全部策略，刷新当前符合条件的个股"
              className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn
                border border-border bg-surface text-xs font-medium text-muted
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer
                disabled:opacity-50 disabled:cursor-wait"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${reloadStrategies.isPending ? 'animate-spin' : ''}`} />
              重载
            </button>
            {asOf && (
              <DatePicker
                value={asOf}
                onChange={handleDateChange}
                min={minDate}
                max={maxDate}
              />
            )}
            {/* 全部切换 */}
            <button
              onClick={() => setShowAll(v => { if (!v) setActiveStrategy(null); return !v })}
              title="显示全部策略个股"
              className={`inline-flex items-center justify-center h-7 w-7 rounded-btn border transition-colors cursor-pointer
                ${showAll
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border bg-surface text-muted hover:text-secondary hover:border-accent/40'
                }`}
            >
              <Network className="h-3.5 w-3.5" />
            </button>
            {/* 卡片尺寸切换 */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['hidden', 'mini', 'normal', 'large'] as const).map(sz => (
                <button
                  key={sz}
                  onClick={() => { setCardSize(sz); storage.screenerCardSize.set(sz) }}
                  className={`h-full px-2 text-[10px] font-medium transition-colors cursor-pointer
                    ${cardSize === sz
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {sz === 'hidden' ? '隐藏' : sz === 'mini' ? '紧凑' : sz === 'normal' ? '标准' : '详细'}
                </button>
              ))}
            </div>
            {/* 策略池按钮 */}
            <button
              onClick={() => setShowPoolDialog(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                border border-border bg-surface text-xs font-medium text-secondary
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5" />
              策略池
              <span className="ml-0.5 min-w-[28px] h-4 flex items-center justify-center rounded-full bg-accent/15 text-accent text-[10px] font-bold">
                {visiblePool.length}/{strategyPresets.length}
              </span>
            </button>
            {/* 创建叠加策略 */}
            <button
              onClick={() => setShowComposite(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                text-xs font-medium text-teal-400 border border-teal-500/20 bg-teal-500/5
                hover:bg-teal-500/15 transition-colors cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5" />
              叠加策略
            </button>
            {/* 创建策略 */}
            <button
              onClick={() => { setBuilderMode('create'); setShowBuilder(true) }}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                text-xs font-medium text-amber-400 border border-amber-400/20 bg-amber-400/5
                hover:bg-amber-400/15 transition-colors cursor-pointer"
            >
              <Sparkles className="h-3.5 w-3.5" />
              创建策略 · AI
            </button>
            {/* 获取策略（占位，敬请期待）— 暂时隐藏 */}
            {SHOW_STRATEGY_STORE && (
              <button
                onClick={() => setShowStore(true)}
                className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                  border border-border bg-surface text-xs font-medium text-secondary
                  hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
              >
                <Store className="h-3.5 w-3.5" />
                获取策略
              </button>
            )}
          </div>
        }
      />

      <div className="px-8 py-4 space-y-3">
        {/* 策略卡片 */}
        {cardSize !== 'hidden' && (
        <section>
          {strategies.isLoading && <div className="text-sm text-muted">加载中…</div>}
          {!strategies.isLoading && displayPool.length === 0 && (
            <div className="text-sm text-muted py-4 text-center border border-dashed border-border rounded-btn">
              {pool.length === 0
                ? '策略池为空，点击右上角「策略池」按钮添加策略'
                : '当前周期筛选下无策略，切换周期筛选或编辑策略池'}
            </div>
          )}
          <div className={cardWrapCls(cardSize)}>
            {displayPool.map(id => {
              const s = strategyMap.get(id)
              if (!s) return null
              return (
                <StrategyCard
                  key={s.id}
                  name={s.name}
                  description={s.description}
                  source={s.source}
                  active={activeStrategy === s.id}
                  count={displayHitCounts[id] ?? hitCounts[id]}
                  countLabel={displayMode === 'preselect' ? '预选' : undefined}
                  expiredCount={displayMode !== 'cached' ? 0 : (expiredCounts[id] ?? 0)}
                  loading={runAll.isPending}
                  cardSize={cardSize}
                  onRun={() => handleRun(s)}
                  disabled={run.isPending && activeStrategy === s.id}
                  onSettings={() => setSettingsStrategyId(s.id)}
                  monitored={strategyMonitorMap.has(s.id)}
                  onToggleMonitor={() => toggleStrategyMonitor(s.id, s.name)}
                  timeframeBadge={s.timeframes?.includes('1m') ? '分钟' : undefined}
                />
              )
            })}
          </div>
        </section>
        )}

        {/* 结果 */}
        <section>
          {run.isError && (
            <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
              {String((run.error as any).message)}
            </div>
          )}

          {resultPanelVisible && (
            <motion.div
              key={showAll ? `all-${asOf}` : `${result!.as_of}-${result!.strategy}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="space-y-3"
            >
              {displayMode === 'preselect' && preselectTotal > 0 && (
                <div className="flex items-start gap-2 rounded-btn border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs text-amber-200">
                  <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" />
                  <div>
                    <div className="font-medium">当前显示的是盘后预选，不是正式双刃合命中</div>
                    <div className="mt-0.5 text-[11px] text-amber-200/70">
                      这些结果仅供次交易日 09:25 前观察，正式结果会在竞价确认后单独切换。
                    </div>
                  </div>
                </div>
              )}
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-foreground flex items-center gap-2">
                  {!showAll && activeStrategy && (
                    <span className="text-secondary">{strategyIdToName[activeStrategy] ?? ''}</span>
                  )}
                  <TrendingUp className="h-4 w-4 text-accent" />
                  {showAll ? '全部' : ''}{resultLabel} <span className="text-accent num">{displayRows.length}</span> 只
                  {filterActive(filter) && displayRows.length !== (showAll ? allRows.length : result!.total) && (
                    <span className="text-muted text-xs">/ {showAll ? allRows.length : result!.total}</span>
                  )}
                  <span className="text-[11px] text-muted font-normal">
                    · {displayPool.length} 策略
                    {!showAll && displayPool.length > 0 && (
                      <> · 共 {displayPool.reduce((sum, id) => sum + (displayHitCounts[id] ?? hitCounts[id] ?? 0), 0)} 只</>
                    )}
                  </span>
                  {!showAll && intradayBuyCount > 0 && (
                    <span className="text-[11px] text-secondary font-normal">
                      · 盘中买点 {intradayBuyCount}
                    </span>
                  )}
                  {displayMode === 'dynamic' ? (
                    <span className="text-[11px] text-secondary">
                      · 动态竞价 {auctionDynamicTotals.final} / 双刃合 {auctionDynamicTotals.candidates}
                      {auctionDynamicFrame?.as_of_time ? ` · ${auctionDynamicFrame.as_of_time}` : ''}
                    </span>
                  ) : displayMode === 'preselect' ? (
                    <span className="text-[11px] text-secondary">
                      · 预选 {preselectTotal} · 等待 {auctionTradeDate} 09:25 竞价确认
                    </span>
                  ) : auctionConfirmationQuery.data && (
                    <span className="text-[11px] text-secondary">
                      · 竞价确认 {auctionConfirmationQuery.data.gate_status === 'confirmed'
                        ? `${Object.values(auctionConfirmationQuery.data.results).reduce((sum, item) => sum + (item.total ?? 0), 0)} / ${Object.values(auctionConfirmationQuery.data.results).reduce((sum, item) => sum + (item.base_total ?? 0), 0)}`
                        : auctionConfirmationQuery.data.gate_status === 'pending_gate'
                          ? '等待 09:25'
                          : `等待 ${auctionTradeDate} 09:25 竞价确认`}
                    </span>
                  )}
                  {runAll.isPending && (
                    <span className="text-[11px] text-muted animate-pulse">扫描中…</span>
                  )}
                </h2>
                <div className="flex items-center gap-3">
                  {(showAll ? allRows.length > 0 : !!result?.rows.length) && (
                    <div className="inline-flex items-stretch h-7 rounded-btn border border-border bg-surface overflow-hidden">
                      <button
                        onClick={() => setShowFilter(v => !v)}
                        className={`inline-flex items-center gap-1.5 px-2.5 text-xs font-medium transition-colors duration-150 cursor-pointer
                          ${filterActive(filter)
                            ? 'bg-accent/15 text-accent'
                            : showFilter
                              ? 'bg-accent/8 text-accent'
                              : 'text-secondary hover:bg-elevated hover:text-foreground'
                          }`}
                      >
                        <Filter className="h-3 w-3" />
                        筛选
                        {filterActive(filter) && (
                          <span className="bg-accent text-base rounded-full min-w-4 h-4 px-1 flex items-center justify-center text-[10px] font-bold leading-none">
                            {countActiveFilters(filter)}
                          </span>
                        )}
                      </button>
                      {filterActive(filter) && (
                        <>
                          <span className="w-px self-stretch my-1 bg-border" />
                          <button
                            onClick={() => {
                              setFilter(defaultFilter)
                              if (activeStrategy) filterMap.current.delete(activeStrategy)
                            }}
                            title="清空筛选条件"
                            className="inline-flex items-center gap-1 px-2 text-muted
                              hover:bg-danger/10 hover:text-danger transition-colors duration-150 cursor-pointer"
                          >
                            <RotateCcw className="h-3 w-3" />
                          </button>
                        </>
                      )}
                    </div>
                  )}
                  {displayRows.length > 0 && (
                    <WatchlistAddMenu
                      onSelect={handleBatchAdd}
                      disabled={batchAdd.isPending}
                      align="right"
                      title="批量加自选"
                      ariaLabel="批量加入自选"
                      triggerClassName="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn
                        border border-accent/40 bg-accent/10 text-accent text-xs font-medium
                        hover:bg-accent/20 disabled:opacity-50 transition-colors duration-150 cursor-pointer"
                    >
                      <Star className="h-3 w-3" />
                      {batchAdd.isPending ? '添加中…' : '批量加自选'}
                    </WatchlistAddMenu>
                  )}
                  <button
                    onClick={() => setCustomizerOpen(true)}
                    title="列表配置"
                    className={`inline-flex items-center justify-center h-7 w-7 rounded-btn border text-xs font-medium transition-colors cursor-pointer
                      ${customizerOpen
                        ? 'border-accent/50 bg-accent/10 text-accent'
                        : 'border-border bg-surface text-secondary hover:text-accent hover:border-accent/50'
                      }`}
                  >
                    <Settings2 className="h-3 w-3" />
                  </button>
                  {batchMsg && (
                    <span className="text-xs text-accent animate-pulse">{batchMsg}</span>
                  )}
                  {!showAll && result && result.elapsed_ms > 0 && (
                    <div className="flex items-center gap-2 text-xs text-muted">
                      <Clock className="h-3 w-3" />
                      <span className="num">{result.elapsed_ms.toFixed(1)} ms</span>
                    </div>
                  )}
                  {/* 分时截断提示: 超数据源批量上限时在工具栏内联显示, 可关闭 */}
                  {intradayTruncated && !intradayCapDismissed && (
                    <span className="inline-flex items-center gap-1 text-xs text-warning/90">
                      分时仅前 {minuteBatchCap}/{allIntradaySymbols.length} · 受数据源批量上限限制
                      <button
                        type="button"
                        onClick={() => setIntradayCapDismissed(true)}
                        className="text-warning/50 hover:text-warning transition-colors"
                        title="关闭提示"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  )}
                </div>
              </div>

              {/* 筛选面板: 只要原始结果有数据就显示 (哪怕筛完后为空, 用户才能改条件) */}
              {showFilter && (showAll ? allRows.length > 0 : !!result?.rows.length) && (
                <FilterPanel
                  value={filter}
                  onChange={setFilter}
                  onClose={() => setShowFilter(false)}
                  onReset={() => {
                    setFilter(defaultFilter)
                    if (activeStrategy) filterMap.current.delete(activeStrategy)
                  }}
                />
              )}

              {displayRows.length === 0 ? (
                <EmptyState
                  icon={ScanSearch}
                  title={
                    displayMode === 'preselect' && preselectTotal > 0
                      ? auctionWaitingHint
                      : displayMode === 'preselect'
                        ? '今日无预选'
                    : auctionDynamicActive && auctionDynamicPayload?.status === 'awaiting_trade'
                      ? `等待 ${auctionTradeDate} 09:25 竞价确认`
                      : auctionDynamicActive
                        ? '动态竞价暂无命中'
                        : auctionConfirmationQuery.data?.gate_status === 'pending_gate'
                      ? `等待 ${auctionTradeDate} 09:25 竞价确认`
                      : auctionConfirmationQuery.data?.gate_status === 'awaiting_trade'
                        ? `等待 ${auctionTradeDate} 09:25 竞价确认`
                        : (filterActive(filter) && (showAll ? allRows.length > 0 : !!result?.rows.length))
                          ? '筛选后无命中'
                          : auctionConfirmationActive
                            ? '竞价确认后无命中'
                            : (filterActive(filter) ? '筛选后无命中' : '今日无命中')
                  }
                  hint={
                    displayMode === 'preselect' && preselectTotal > 0
                      ? '预选不是最终结果，次交易日竞价确认后会自动切换。'
                      : displayMode === 'preselect'
                        ? '严格策略和盘后放宽预选都没有候选，可等盘后管道完成后重载。'
                    : auctionDynamicActive && auctionDynamicPayload?.status === 'awaiting_trade'
                      ? '盘后候选已就绪，09:25 后会自动切到竞价确认结果。'
                      : auctionDynamicActive
                        ? '当前秒的双刃合动态候选和竞价确认条件都没有留下结果。'
                        : auctionConfirmationQuery.data?.gate_status === 'pending_gate'
                      ? '盘后候选已就绪，09:25 后会自动切到竞价确认结果。'
                      : auctionConfirmationQuery.data?.gate_status === 'awaiting_trade'
                        ? '盘后候选已就绪，09:25 后会自动切到竞价确认结果。'
                        : (filterActive(filter) && (showAll ? allRows.length > 0 : !!result?.rows.length))
                          ? '当前筛选条件过严, 试试放宽或重置筛选。'
                          : auctionConfirmationActive
                            ? '候选已做竞价 / 开盘确认，但没有保留下来的结果。'
                            : (filterActive(filter)
                              ? '当前筛选条件过严, 试试放宽或重置筛选。'
                              : '可能数据未跑盘后管道,或策略条件过于严苛。试试 POST /api/pipeline/run。')
                  }
                />
              ) : (
                <>
                  <ScreenerTable
                    rows={displayRows}
                    columns={columns}
                    strategyIdToName={strategyIdToName}
                    strategyLabelSuffix={displayMode === 'preselect' ? '盘后预选' : undefined}
                    symbolStrategyMap={symbolStrategyMap}
                    activeStrategy={activeStrategy}
                    watchlistSet={watchlistSet}
                    purchaseMarks={purchaseMarks}
                    purchaseSignalDate={!showAll && activeStrategy ? (result?.as_of || asOf) : undefined}
                    purchaseMarkPending={togglePurchaseMark.isPending}
                    onTogglePurchaseMark={(strategyId, symbol, signalDate, signalPrice, signalScore, signalChangePct, marked) =>
                      togglePurchaseMark.mutate({
                        strategyId,
                        symbol,
                        signalDate,
                        signalPrice,
                        signalScore,
                        signalChangePct,
                        marked,
                      })}
                    onPreview={(symbol, name) => { setPreviewSymbol(symbol); setPreviewName(name) }}
                    onAddToWatchlist={(symbol, groupId) => toggleWatchlist.mutate({ symbol, action: 'add', groupId })}
                    onRemoveFromWatchlist={symbol => toggleWatchlist.mutate({ symbol, action: 'remove' })}
                    watchlistPending={toggleWatchlist.isPending}
                    klineData={klineData}
                    dailyKChartVisible={dailyKChartVisible}
                    onToggleDailyKChart={toggleDailyKChart}
                    minuteData={minuteData}
                    intradayChartVisible={intradayChartVisible}
                    onToggleIntradayChart={toggleIntradayChart}
                    intradayAutoRefresh={intradayRefreshEnabled && realtimeRunning}
                    onRefreshIntraday={() => minuteBatch.refetch()}
                    intradayRefreshing={minuteBatch.isFetching}
                    sort={sort}
                    onSortToggle={toggle}
                  />
                  {activePreselectRows.length > 0 && displayMode !== 'preselect' && (
                    <section className="mt-3 rounded-xl border border-amber-500/25 bg-amber-500/5">
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-500/15 px-4 py-2.5">
                        <div className="flex items-center gap-2 text-xs">
                          <span className="font-medium text-amber-200">盘后预选</span>
                          <span className="num text-amber-300">{activePreselectRows.length} 只</span>
                          <span className="text-[10px] text-amber-200/60">非正式命中 · 等待 {auctionTradeDate} 09:25 竞价确认</span>
                        </div>
                        <span className="text-[10px] text-amber-200/60">仅作次日观察</span>
                      </div>
                      <div className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                        {activePreselectRows.map((row: any) => (
                          <button
                            key={row.symbol}
                            type="button"
                            onClick={() => { setPreviewSymbol(row.symbol); setPreviewName(row.name ?? '') }}
                            className="rounded-lg border border-border/50 bg-surface/50 px-3 py-2 text-left transition hover:border-amber-400/40 hover:bg-amber-500/10"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate text-xs font-medium text-foreground">{row.name || row.symbol}</span>
                              <span className="shrink-0 text-[10px] text-amber-200">观察</span>
                            </div>
                            <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted">
                              <span className="font-mono">{row.symbol}</span>
                              <span className="num">价 {row.close != null ? Number(row.close).toFixed(2) : '—'}</span>
                              <span className={`num ${Number(row.change_pct) >= 0 ? 'text-success' : 'text-danger'}`}>
                                {row.change_pct != null ? `${(Number(row.change_pct) * 100).toFixed(2)}%` : '—'}
                              </span>
                            </div>
                          </button>
                        ))}
                      </div>
                    </section>
                  )}
                </>
              )}
            </motion.div>
          )}

          {!showAll && !result && !run.isPending && (
            <div className="flex flex-col items-center justify-center py-16 gap-4">
              <div className="w-16 h-16 rounded-2xl bg-accent/5 border border-border flex items-center justify-center">
                <ScanSearch className="h-7 w-7 text-accent/40" />
              </div>
              <div className="flex flex-col items-center gap-1.5">
                <span className="text-sm text-secondary">点击策略卡片查看选股结果</span>
                <span className="text-[11px] text-muted">若提示 enriched 表无数据，请先运行盘后管道</span>
              </div>
            </div>
          )}
        </section>
      </div>

      <ListColumnCustomizer
        columns={columns}
        groups={SCREENER_COLUMN_GROUPS}
        onChange={handleColumnsChange}
        open={customizerOpen}
        onClose={() => setCustomizerOpen(false)}
        title="自定义策略结果列"
        builtinSectionLabel="策略内置列"
        extColumnAlign="center"
      />

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewName}
        onClose={closePreview}
      />

      <StrategySettingsDialog
        strategyId={settingsStrategyId}
        onClose={() => setSettingsStrategyId(null)}
        onSaved={(limit) => {
          if (settingsStrategyId) {
            setStrategyLimits(prev => ({ ...prev, [settingsStrategyId]: limit }))
            // 按策略自身周期重跑: 日线用当前 asOf, 分钟实时单跑交后端取最新分区
            const tf = strategyMap.get(settingsStrategyId)?.timeframes?.includes('1m') ? '1m' as const : '1d' as const
            run.mutate({ id: settingsStrategyId, date: tf === '1m' ? '' : asOf, timeframe: tf })
          }
        }}
        onAiModify={async () => {
          if (!settingsStrategyId) return
          try {
            const [src, detail] = await Promise.all([
              api.strategyGetSource(settingsStrategyId),
              api.strategyGet(settingsStrategyId),
            ])
            storage.strategyModify.set({
              name: detail.name ?? '',
              description: detail.description ?? '',
              direction: 'long',
              rules: storage.strategyRules.get({})[settingsStrategyId] ?? '',
              code: src.code, step: 2, strategyId: settingsStrategyId, source: src.source as any,
            })
            setSettingsStrategyId(null)
            setBuilderMode('modify')
            setShowBuilder(true)
          } catch {}
        }}
        onDeleted={() => {
          if (settingsStrategyId) {
            removeFromPool(settingsStrategyId)
            const rules = storage.strategyRules.get({})
            delete rules[settingsStrategyId]; storage.strategyRules.set(rules)
            setStrategyLimits(prev => { const next = {...prev}; delete next[settingsStrategyId]; return next })
            qc.invalidateQueries({ queryKey: ['screener-strategies'] })
          }
        }}
      />

      {showPoolDialog && (
        <StrategyPoolDialog
          pool={pool}
          onConfirm={(newPool) => {
            reorderPool(newPool)
          }}
          onClose={() => setShowPoolDialog(false)}
        />
      )}
      <StrategyBuilderDialog
        open={showBuilder}
        onClose={() => setShowBuilder(false)}
        mode={builderMode}
        existingStrategyIds={allStrategyIds}
        onSavedId={async id => {
          const data = await qc.fetchQuery({ queryKey: QK.screenerStrategies('all'), queryFn: () => api.screenerStrategies(), staleTime: 0 })
          if (!data.presets.some(s => s.id === id)) {
            throw new Error(`策略 ${id} 已保存但未加载，请检查策略代码`)
          }
          addToPool(id)
        }}
      />

      <CompositeStrategyDialog
        open={showComposite}
        onClose={() => setShowComposite(false)}
        onSavedId={async id => {
          await qc.fetchQuery({ queryKey: QK.screenerStrategies('all'), queryFn: () => api.screenerStrategies(), staleTime: 0 })
          addToPool(id)
        }}
      />

      <StrategyStoreDialog
        open={showStore}
        onClose={() => setShowStore(false)}
      />

      {activeStrategy && (
        <button
          type="button"
          onClick={() => setShowHistoryDialog(true)}
          title={`查看${strategyIdToName[activeStrategy] ?? activeStrategy}推荐历史`}
          className="fixed bottom-5 right-5 z-30 inline-flex items-center gap-2 rounded-full border border-accent/30 bg-surface/95 px-4 py-2.5 text-xs font-medium text-foreground shadow-xl shadow-black/20 backdrop-blur-md transition hover:border-accent/60 hover:bg-accent/10"
        >
          <History className="h-3.5 w-3.5 text-accent" />
          <span>推荐历史</span>
          <span className="rounded-full bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent num">
            {strategyHistoryQuery.data?.total ?? 0}
          </span>
        </button>
      )}

      {showHistoryDialog && activeStrategy && (
        <Modal
          onClose={() => setShowHistoryDialog(false)}
          labelledBy="strategy-history-title"
          panelClassName="w-[min(920px,94vw)] max-h-[82vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
        >
          <div className="flex items-center justify-between border-b border-border/50 px-5 py-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <History className="h-4 w-4 shrink-0 text-accent" />
              <div className="min-w-0">
                <div id="strategy-history-title" className="truncate text-sm font-semibold text-foreground">
                  {strategyIdToName[activeStrategy] ?? activeStrategy} · 推荐历史
                </div>
                <div className="mt-0.5 text-[10px] text-muted">
                  仅展示当前策略推荐事件及其关联操作 · 盘中买卖信号不等同正式命中 · 最近 {recommendationHistoryEvents.length} 条
                </div>
              </div>
            </div>
            <button
              type="button"
              aria-label="关闭"
              onClick={() => setShowHistoryDialog(false)}
              className="rounded-lg p-1.5 transition hover:bg-elevated"
            >
              <X className="h-4 w-4 text-muted" />
            </button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {strategyHistoryQuery.isLoading ? (
              <div className="flex items-center justify-center py-16 text-xs text-muted">历史加载中…</div>
            ) : historyPageEvents.length === 0 ? (
              <div className="flex items-center justify-center py-16 text-xs text-muted">当前策略暂无历史记录</div>
            ) : (
              <div className="space-y-2">
                {historyPageEvents.map(event => {
                  const isRejected = event.event_type === 'auction_rejected'
                  const isConfirmed = event.event_type === 'auction_confirmed'
                  const isBuy = event.event_type === 'buy_signal'
                  const isSell = event.event_type === 'sell_signal'
                  const hasEventTime = isBuy || isSell
                    || isConfirmed || isRejected
                    || event.event_type === 'pool_entry'
                    || event.event_type === 'pool_exit'
                  const tone = isRejected || isSell
                    ? 'text-danger bg-danger/10 border-danger/20'
                    : isConfirmed || isBuy
                      ? 'text-success bg-success/10 border-success/20'
                      : 'text-accent bg-accent/10 border-accent/20'
                  return (
                    <div key={event.event_key} className="rounded-xl border border-border/40 bg-elevated/30 px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${tone}`}>
                          {HISTORY_EVENT_LABELS[event.event_type] ?? event.event_type}
                        </span>
                        <span className="font-medium text-foreground">{event.name || event.symbol}</span>
                        <span className="font-mono text-[10px] text-muted">{event.symbol}</span>
                        <span className="text-[10px] text-muted">
                          {event.signal_date}{event.trade_date && event.trade_date !== event.signal_date ? ` → ${event.trade_date}` : ''}
                        </span>
                        {hasEventTime && (
                          <span className="font-mono text-[10px] text-secondary">
                            时间 {formatHistoryTime(event.ts)}
                          </span>
                        )}
                        {event.price != null && <span className="num text-secondary">价 {event.price.toFixed(2)}</span>}
                        {event.change_pct != null && (
                          <span className={`num ${event.change_pct >= 0 ? 'text-success' : 'text-danger'}`}>
                            {(event.change_pct * 100).toFixed(2)}%
                          </span>
                        )}
                      </div>
                      {(event.reason || event.signals?.length) && (
                        <div className="mt-1 text-[11px] leading-5 text-secondary">
                          {event.reason || event.signals?.join('、')}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between border-t border-border/50 px-5 py-3 text-[11px] text-muted">
            <span>第 {Math.min(historyPage + 1, historyPageCount)} / {historyPageCount} 页</span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => setHistoryPage(page => Math.max(page - 1, 0))}
                disabled={historyPage <= 0}
                className="inline-flex items-center gap-1 rounded-btn border border-border px-2.5 py-1.5 transition hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35"
              >
                <ChevronLeft className="h-3.5 w-3.5" />上一页
              </button>
              <button
                type="button"
                onClick={() => setHistoryPage(page => Math.min(page + 1, historyPageCount - 1))}
                disabled={historyPage >= historyPageCount - 1}
                className="inline-flex items-center gap-1 rounded-btn border border-border px-2.5 py-1.5 transition hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35"
              >
                下一页<ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  )
}
