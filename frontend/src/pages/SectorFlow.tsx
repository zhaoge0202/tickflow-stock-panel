import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'framer-motion'
import {
  Activity,
  CalendarDays,
  ChartScatter,
  Clock3,
  LineChart,
  Layers3,
  Landmark,
  Pause,
  Play,
  RefreshCw,
  Search,
  Settings2,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { AnalysisConfigDialog, PresetFetchState, type AnalysisFieldConfig } from '@/components/analysis-shared'
import { SectorFlowBubbles, type SectorFlowItem } from '@/components/SectorFlowBubbles'
import { SectorFlowTrendChart } from '@/components/SectorFlowTrendChart'
import { api, type ExtDataConfig, type MarketSnapshotRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { useQuoteStatus } from '@/lib/useSharedQueries'
import { resolveDimension, type DimensionGroup, type StockRow } from '@/lib/analysis-adapter'
import { fmtBigNum, fmtPct, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'

const PAGE_LIMIT = 12000
const TIMELINE_STEP_SECONDS = 60
const PLAYBACK_INTERVAL_MS = 1000
const LIVE_MARKET_PHASES = new Set(['preopen', 'morning', 'morning_final', 'pre_afternoon', 'afternoon', 'close_final'])

type SectorKind = 'concept' | 'industry'
type TimeMode = 'live' | 'replay' | 'eod'
type ViewMode = 'bubble' | 'strength' | 'main_flow'
type SortMode = 'heat' | 'avgPct' | 'amount' | 'down' | 'count'

interface SectorFlowSettings {
  kind?: SectorKind
  concept?: AnalysisFieldConfig
  industry?: AnalysisFieldConfig
  date?: string
  asOfTs?: number | null
  timeMode?: TimeMode
  viewMode?: ViewMode
  maxItems?: number
  sortMode?: SortMode
}

interface EnrichedStock extends MarketSnapshotRow {
  leaderScore: number
}

interface SectorStat extends SectorFlowItem {
  stocks: EnrichedStock[]
  medianPct: number | null
  upRate: number
  avgTurnover: number | null
  avgVolRatio: number | null
  leader: EnrichedStock | null
}

interface LastGoodStats {
  contextKey: string
  stats: SectorStat[]
  marketCount: number
}

const KIND_META: Record<SectorKind, {
  label: string
  title: string
  icon: typeof Layers3
  keywords: string[]
  candidates: string[]
  presetId: string
}> = {
  concept: {
    label: '概念',
    title: '概念动能',
    icon: Layers3,
    keywords: ['concept', '概念', 'theme', '题材', '板块'],
    candidates: ['concept', '概念', 'theme', '题材', '板块', 'concept_name', '概念名称'],
    presetId: 'ext_gn_ths',
  },
  industry: {
    label: '行业',
    title: '行业动能',
    icon: Landmark,
    keywords: ['industry', '行业', 'sector', '申万', '中信'],
    candidates: ['industry', '行业', 'sector', '申万', '中信', '行业名称', 'industry_name', 'sector_name'],
    presetId: 'ext_hy_ths',
  },
}

function loadSettings(): SectorFlowSettings {
  const saved = storage.sectorFlowConfig.get({}) as SectorFlowSettings
  const today = cnToday()
  if (saved.date && saved.date !== today) {
    return { ...saved, date: today, timeMode: 'live', asOfTs: null }
  }
  return saved
}

function saveSettings(next: SectorFlowSettings) {
  storage.sectorFlowConfig.set(next)
}

function cnToday() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

function formatTs(ts: number | null | undefined) {
  if (ts == null || !Number.isFinite(ts)) return '最新'
  return new Date(ts).toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function pickBestConfig(configs: ExtDataConfig[], keywords: string[]) {
  let best = ''
  let bestScore = 0
  for (const c of configs) {
    const haystack = [c.id, c.label, c.description ?? '', ...c.fields.flatMap(f => [f.name, f.label])].join(' ').toLowerCase()
    const score = keywords.reduce((n, k) => n + (haystack.includes(k.toLowerCase()) ? 1 : 0), 0)
    if (score > bestScore) {
      best = c.id
      bestScore = score
    }
  }
  return best
}

function symbolKeys(symbol: unknown): string[] {
  const raw = String(symbol ?? '').trim()
  if (!raw) return []
  const plain = raw.replace(/\.\w+$/, '')
  return Array.from(new Set([raw, plain]))
}

function buildMarketMap(rows: MarketSnapshotRow[]) {
  const map = new Map<string, MarketSnapshotRow>()
  for (const row of rows) {
    for (const key of symbolKeys(row.symbol)) map.set(key, row)
  }
  return map
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function avg(values: number[]) {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null
}

function median(values: number[]) {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

function clamp01(v: number) {
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(1, v))
}

function leaderScore(stock: MarketSnapshotRow) {
  const pct = num(stock.change_pct) ?? 0
  const turnover = num(stock.turnover_rate) ?? 0
  const amount = num(stock.amount) ?? 0
  const cap = num(stock.float_market_cap) ?? num(stock.market_cap) ?? 0
  const volRatio = num(stock.realtime_vol_ratio) ?? num(stock.vol_ratio_5d) ?? 1
  const boards = num(stock.consecutive_limit_ups) ?? 0
  return (
    clamp01((pct + 0.02) / 0.12) * 0.36 +
    clamp01(Math.log1p(Math.max(turnover, 0)) / Math.log1p(30)) * 0.18 +
    clamp01(Math.log1p(Math.max(amount, 0)) / Math.log1p(20_000_000_000)) * 0.22 +
    clamp01(Math.log1p(Math.max(cap, 0)) / Math.log1p(300_000_000_000)) * 0.12 +
    clamp01((volRatio - 1) / 4) * 0.08 +
    clamp01(boards / 5) * 0.04
  ) * 100
}

function enrichStock(stock: StockRow, marketMap: Map<string, MarketSnapshotRow>): EnrichedStock {
  const market = symbolKeys(stock.symbol ?? stock.code).map(k => marketMap.get(k)).find(Boolean) ?? {}
  const merged = { ...stock, ...market } as MarketSnapshotRow & StockRow
  const symbol = String(merged.symbol ?? stock.symbol ?? stock.code ?? '')
  return {
    ...merged,
    symbol,
    name: merged.name ?? String(stock.name ?? stock['股票简称'] ?? ''),
    leaderScore: leaderScore(merged),
  }
}

function calcSectorStat(group: DimensionGroup, marketMap: Map<string, MarketSnapshotRow>): SectorStat {
  const seen = new Set<string>()
  const stocks = group.stocks
    .map(s => enrichStock(s, marketMap))
    .filter(s => {
      const key = String(s.symbol ?? '')
      if (!key || seen.has(key)) return false
      seen.add(key)
      return true
    })

  const pctValues = stocks.map(s => num(s.change_pct)).filter((v): v is number => v != null)
  const turnoverValues = stocks.map(s => num(s.turnover_rate)).filter((v): v is number => v != null)
  const volValues = stocks.map(s => num(s.realtime_vol_ratio) ?? num(s.vol_ratio_5d)).filter((v): v is number => v != null)
  const totalAmount = stocks.reduce((sum, s) => sum + (num(s.amount) ?? 0), 0)
  const upCount = pctValues.filter(v => v > 0).length
  const downCount = pctValues.filter(v => v < 0).length
  const avgPct = avg(pctValues)
  const upRate = pctValues.length ? upCount / pctValues.length : 0
  const leader = stocks.length ? [...stocks].sort((a, b) => b.leaderScore - a.leaderScore)[0] : null
  const strongCount = pctValues.filter(v => v >= 0.05).length
  const amountScore = clamp01(Math.log1p(totalAmount) / Math.log1p(80_000_000_000))
  const avgPart = clamp01(((avgPct ?? 0) + 0.02) / 0.09)
  const upPart = clamp01((upRate - 0.35) / 0.55)
  const strongPart = stocks.length ? clamp01(strongCount / Math.max(1, stocks.length * 0.18)) : 0
  const leaderPart = clamp01((leader?.leaderScore ?? 0) / 100)
  const heatScore = (avgPart * 0.38 + upPart * 0.2 + strongPart * 0.16 + amountScore * 0.12 + leaderPart * 0.14) * 100

  return {
    key: group.key,
    stocks,
    count: stocks.length,
    avgPct,
    medianPct: median(pctValues),
    upCount,
    downCount,
    totalAmount,
    heatScore,
    upRate,
    avgTurnover: avg(turnoverValues),
    avgVolRatio: avg(volValues),
    leader,
  }
}

function industryLevelName(key: string, level: 1 | 2 | 3) {
  const parts = key.split('-').map(s => s.trim()).filter(Boolean)
  return parts[level - 1] || parts[parts.length - 1] || key
}

function groupByIndustryLevel(groups: DimensionGroup[], level: 1 | 2 | 3): DimensionGroup[] {
  const map = new Map<string, DimensionGroup>()
  for (const group of groups) {
    const key = industryLevelName(group.key, level)
    const existing = map.get(key)
    if (existing) {
      existing.stocks.push(...group.stocks)
      existing.count = existing.stocks.length
    } else {
      map.set(key, { key, count: group.stocks.length, stocks: [...group.stocks], metrics: { ...group.metrics } })
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count)
}

function sortStats(mode: SortMode) {
  return (a: SectorStat, b: SectorStat) => {
    switch (mode) {
      case 'avgPct':
        return (b.avgPct ?? -Infinity) - (a.avgPct ?? -Infinity)
      case 'amount':
        return b.totalAmount - a.totalAmount
      case 'down':
        return (a.avgPct ?? Infinity) - (b.avgPct ?? Infinity)
      case 'count':
        return b.count - a.count
      case 'heat':
      default:
        return b.heatScore - a.heatScore
    }
  }
}

function modeLabel(mode: TimeMode, snapshotMode?: string) {
  if (mode === 'live') return snapshotMode === 'eod_fallback' ? '实时回退' : '实时'
  if (mode === 'replay') return snapshotMode?.startsWith('intraday') ? '盘中回放' : '回放回退'
  return '收盘'
}

export function SectorFlow() {
  const [settings, setSettings] = useState<SectorFlowSettings>(loadSettings)
  const [showConfig, setShowConfig] = useState(false)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [isPlaying, setIsPlaying] = useState(false)
  const [lastGoodStats, setLastGoodStats] = useState<LastGoodStats | null>(null)
  const [selectedSeriesKeys, setSelectedSeriesKeys] = useState<string[]>([])
  const queryClient = useQueryClient()
  const quoteStatus = useQuoteStatus()

  const kind = settings.kind ?? 'concept'
  const meta = KIND_META[kind]
  const fieldConfig = settings[kind] ?? {}
  const today = cnToday()
  const selectedDate = settings.date ?? ''
  const isSelectedToday = !selectedDate || selectedDate === today
  const marketPhase = quoteStatus.data?.market_phase ?? ''
  const isLiveMarketPhase = Boolean(
    quoteStatus.data?.is_trading_hours ||
    quoteStatus.data?.is_polling_window ||
    LIVE_MARKET_PHASES.has(marketPhase),
  )
  const canLive = Boolean(
    quoteStatus.data &&
    quoteStatus.data.enabled !== false &&
    quoteStatus.data.realtime_allowed !== false &&
    isLiveMarketPhase,
  )
  const timeMode = settings.timeMode ?? (canLive && isSelectedToday ? 'live' : 'eod')
  const viewMode = settings.viewMode ?? 'strength'
  const sortMode = settings.sortMode ?? 'heat'
  const maxItems = settings.maxItems ?? 64

  const patchSettings = (patch: Partial<SectorFlowSettings>, options?: { persist?: boolean }) => {
    setSettings(prev => {
      const next = { ...prev, ...patch }
      if (options?.persist !== false) saveSettings(next)
      return next
    })
  }

  const configsQuery = useQuery({ queryKey: QK.extData, queryFn: api.extDataList })
  const marketDatesQuery = useQuery({ queryKey: QK.marketDates(120), queryFn: () => api.marketDates(120), staleTime: 60_000 })

  const availableConfigs = configsQuery.data?.items ?? []
  const preferredConfigId = fieldConfig.configId || pickBestConfig(availableConfigs, meta.keywords)
  const preferredConfig = availableConfigs.find(c => c.id === preferredConfigId)
  const activeConfigId = preferredConfig ? preferredConfigId : pickBestConfig(availableConfigs, meta.keywords)
  const activeConfig = availableConfigs.find(c => c.id === activeConfigId)
  const statsContextKey = [
    kind,
    activeConfigId || '',
    selectedDate || 'latest',
    fieldConfig.dimensionField || '',
    fieldConfig.hierarchyLevel ?? '',
  ].join('|')

  const dates = useMemo(() => {
    const set = new Set<string>(marketDatesQuery.data?.dates ?? [])
    set.add(today)
    if (selectedDate) set.add(selectedDate)
    return [...set].sort((a, b) => b.localeCompare(a))
  }, [marketDatesQuery.data?.dates, selectedDate, today])

  useEffect(() => {
    if (settings.date) return
    if (canLive) {
      patchSettings({ date: today, timeMode: 'live', asOfTs: null })
      return
    }
    if (!marketDatesQuery.data?.latest) return
    patchSettings({ date: marketDatesQuery.data.latest, timeMode: 'eod', asOfTs: null })
  }, [settings.date, marketDatesQuery.data?.latest, canLive, today])

  useEffect(() => {
    if (timeMode !== 'live') return
    if (canLive && !isSelectedToday) {
      patchSettings({ date: today, asOfTs: null })
      return
    }
    if (!canLive) {
      patchSettings({ timeMode: 'eod', asOfTs: null })
    }
  }, [canLive, isSelectedToday, timeMode, today])

  const rowsQuery = useQuery({
    queryKey: QK.extDataRows(activeConfigId, selectedDate || undefined, PAGE_LIMIT),
    queryFn: () => api.extDataRows(activeConfigId, { date: selectedDate || undefined, limit: PAGE_LIMIT }),
    enabled: !!activeConfigId,
  })

  const timelineQuery = useQuery({
    queryKey: QK.marketIntradayTimeline(selectedDate || null, TIMELINE_STEP_SECONDS),
    queryFn: () => api.marketIntradayTimeline({ asOf: selectedDate || undefined, stepSeconds: TIMELINE_STEP_SECONDS }),
    enabled: !!selectedDate,
    staleTime: 20_000,
    refetchInterval: query => {
      const data = query.state.data
      const status = data?.backfill?.status ?? data?.backfill_status
      return isSelectedToday || status === 'queued' || status === 'running' ? 30_000 : false
    },
  })

  const seriesMetric = viewMode === 'main_flow' ? 'main_flow' : 'strength'
  const seriesDate = selectedDate || today
  const seriesQuery = useQuery({
    queryKey: QK.sectorFlowSeries(kind, seriesMetric, seriesDate, TIMELINE_STEP_SECONDS, kind === 'industry' ? fieldConfig.hierarchyLevel ?? 2 : null),
    queryFn: () => api.sectorFlowSeries({
      kind,
      metric: seriesMetric,
      date: seriesDate,
      stepSeconds: TIMELINE_STEP_SECONDS,
      limit: 48,
      level: kind === 'industry' ? fieldConfig.hierarchyLevel ?? 2 : null,
    }),
    enabled: viewMode !== 'bubble' && !!seriesDate,
    staleTime: isSelectedToday ? 12_000 : 60_000,
    refetchInterval: viewMode !== 'bubble' && isSelectedToday ? 20_000 : false,
    placeholderData: previous => previous,
  })

  const realtimeDisabledReason = !quoteStatus.data
    ? '正在检查实时行情状态'
    : quoteStatus.data.enabled === false || quoteStatus.data.running === false
      ? '实时行情轮询未开启，请到设置中开启'
      : !canLive
        ? '当前不在实时行情窗口'
        : null

  const points = timelineQuery.data?.points ?? []
  const hasTimeline = points.length > 0
  const canReplay = points.length > 1

  useEffect(() => {
    const status = timelineQuery.data?.backfill_status
    if (status !== 'queued' && status !== 'running') return
    const timer = window.setTimeout(() => {
      timelineQuery.refetch()
    }, 5_000)
    return () => window.clearTimeout(timer)
  }, [timelineQuery.data?.backfill_status, selectedDate])

  const selectedPointIndex = useMemo(() => {
    if (!points.length) return 0
    const target = settings.asOfTs ?? points[points.length - 1]
    let best = 0
    let bestDiff = Infinity
    points.forEach((p, i) => {
      const diff = Math.abs(p - target)
      if (diff < bestDiff) {
        best = i
        bestDiff = diff
      }
    })
    return best
  }, [points, settings.asOfTs])

  useEffect(() => {
    if (timeMode !== 'replay' || !points.length || settings.asOfTs) return
    patchSettings({ asOfTs: points[0] })
  }, [timeMode, points.length, settings.asOfTs])

  useEffect(() => {
    if (timeMode !== 'replay' || !canReplay) setIsPlaying(false)
  }, [timeMode, canReplay])

  useEffect(() => {
    if (!isPlaying || timeMode !== 'replay' || !canReplay) return
    if (selectedPointIndex >= points.length - 1) {
      setIsPlaying(false)
      return
    }
    const timer = window.setTimeout(() => {
      patchSettings({
        timeMode: 'replay',
        asOfTs: points[selectedPointIndex + 1] ?? points[points.length - 1],
      }, { persist: false })
    }, PLAYBACK_INTERVAL_MS)
    return () => window.clearTimeout(timer)
  }, [isPlaying, timeMode, canReplay, selectedPointIndex, points])

  const snapshotDate = timeMode === 'live'
    ? today
    : selectedDate || null
  const snapshotKeyTs = timeMode === 'live'
    ? 'live'
    : timeMode === 'replay'
      ? settings.asOfTs ?? points[points.length - 1] ?? null
      : null

  const snapshotQuery = useQuery({
    queryKey: QK.marketSnapshot(snapshotDate, snapshotKeyTs),
    queryFn: () => api.marketSnapshot({
      asOf: snapshotDate || undefined,
      asOfTs: timeMode === 'live'
        ? Date.now()
        : timeMode === 'replay'
          ? settings.asOfTs ?? points[points.length - 1] ?? undefined
          : undefined,
    }),
    enabled: !!activeConfig,
    staleTime: timeMode === 'live' ? 3_000 : 60_000,
    refetchInterval: timeMode === 'live' ? 8_000 : false,
    placeholderData: previous => previous,
  })

  const fetchMutation = useMutation({
    mutationFn: () => api.extDataPresetFetch(meta.presetId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.extData })
      queryClient.invalidateQueries({ queryKey: QK.extDataRows(meta.presetId, selectedDate || undefined, PAGE_LIMIT) })
    },
  })

  const marketMap = useMemo(() => buildMarketMap(snapshotQuery.data?.rows ?? []), [snapshotQuery.data?.rows])
  const resolved = useMemo(
    () => resolveDimension(
      rowsQuery.data,
      activeConfig,
      fieldConfig.dimensionField ? [fieldConfig.dimensionField, ...meta.candidates] : meta.candidates,
    ),
    [rowsQuery.data, activeConfig, fieldConfig.dimensionField, meta.candidates],
  )

  const groups = useMemo(() => {
    if (kind !== 'industry') return resolved.groups
    return groupByIndustryLevel(resolved.groups, fieldConfig.hierarchyLevel ?? 2)
  }, [kind, resolved.groups, fieldConfig.hierarchyLevel])

  const stats = useMemo(() => {
    return groups
      .map(g => calcSectorStat(g, marketMap))
      .filter(s => s.count > 0 && (s.avgPct != null || s.totalAmount > 0))
  }, [groups, marketMap])

  useEffect(() => {
    if (!stats.length) return
    setLastGoodStats({
      contextKey: statsContextKey,
      stats,
      marketCount: snapshotQuery.data?.count ?? snapshotQuery.data?.rows.length ?? 0,
    })
  }, [stats, statsContextKey, snapshotQuery.data?.count, snapshotQuery.data?.rows.length])

  useEffect(() => {
    if (viewMode === 'bubble') return
    const sectors = seriesQuery.data?.sectors ?? []
    if (!sectors.length) {
      setSelectedSeriesKeys([])
      return
    }
    setSelectedSeriesKeys(prev => {
      const available = new Set(sectors.map(item => item.key))
      const kept = prev.filter(key => available.has(key))
      return kept.length ? kept : sectors.slice(0, 8).map(item => item.key)
    })
  }, [viewMode, seriesQuery.data?.sectors])

  const snapshotRowCount = snapshotQuery.data?.rows.length ?? 0
  const backfillStatus = timelineQuery.data?.backfill?.status ?? timelineQuery.data?.backfill_status ?? ''
  const usingLastGoodStats =
    stats.length === 0 &&
    groups.length > 0 &&
    snapshotRowCount === 0 &&
    lastGoodStats?.contextKey === statsContextKey
  const displayStats = usingLastGoodStats ? lastGoodStats.stats : stats

  const filteredStats = useMemo(() => {
    const q = search.trim().toLowerCase()
    const base = q ? displayStats.filter(s => s.key.toLowerCase().includes(q)) : displayStats
    return [...base].sort(sortStats(sortMode))
  }, [displayStats, search, sortMode])

  const selected = filteredStats.find(s => s.key === selectedKey) ?? filteredStats[0] ?? null
  const breadth = useMemo(() => {
    const priced = displayStats.filter(s => s.avgPct != null)
    return {
      up: priced.filter(s => (s.avgPct ?? 0) > 0).length,
      down: priced.filter(s => (s.avgPct ?? 0) < 0).length,
      flat: priced.filter(s => (s.avgPct ?? 0) === 0).length,
      amount: displayStats.reduce((sum, s) => sum + s.totalAmount, 0),
    }
  }, [displayStats])

  const displayDate = (snapshotQuery.data?.as_of ?? selectedDate) || '最新'
  const displayTime = timeMode === 'eod' ? '收盘' : formatTs(snapshotQuery.data?.as_of_ts ?? settings.asOfTs)
  const phaseText = isSelectedToday && quoteStatus.data?.market_phase ? ` · ${quoteStatus.data.market_phase}` : ''
  const needsPresetFetch = !!activeConfig && activeConfig.id === meta.presetId && !rowsQuery.isLoading && (rowsQuery.data?.total ?? 0) === 0
  const ModeIcon = meta.icon
  const replayRangeText = hasTimeline
    ? `${formatTs(points[0])} - ${formatTs(points[points.length - 1])} · ${selectedPointIndex + 1}/${points.length}`
    : timelineQuery.isFetching
      ? '正在检查盘中回放数据'
      : timelineQuery.data?.message || '该日期暂无盘中回放点'
  const replayDataText = timelineQuery.data?.backfill_status === 'materialized'
    ? `已用本地分钟K补齐 ${timelineQuery.data.backfill?.symbols ?? timelineQuery.data.symbol_count ?? 0} 只`
    : backfillStatus === 'queued' || backfillStatus === 'running'
      ? '正在后台补历史分钟数据'
    : timelineQuery.data?.backfill_status === 'partial_ticks'
      ? `盘中 tick 不完整，仅覆盖 ${timelineQuery.data.symbol_count ?? 0} 只`
    : timelineQuery.data?.backfill_status === 'missing_minute'
      ? '缺少盘中 tick 和本地分钟K'
      : timelineQuery.data?.backfill_status === 'missing_today_ticks'
        ? '今日盘中采集尚未写入'
      : ''
  const marketCountText = usingLastGoodStats
    ? `${lastGoodStats?.marketCount ?? 0} 只`
    : `${snapshotQuery.data?.count ?? snapshotQuery.data?.rows.length ?? 0} 只`
  const bubbleFrameKey = timeMode === 'replay'
    ? settings.asOfTs ?? snapshotQuery.data?.as_of_ts ?? selectedPointIndex
    : null

  const startPlayback = () => {
    if (!canReplay) return
    const shouldRestart = timeMode !== 'replay' || selectedPointIndex >= points.length - 1
    patchSettings({
      timeMode: 'replay',
      asOfTs: shouldRestart ? points[0] : settings.asOfTs ?? points[selectedPointIndex] ?? points[0],
    })
    setIsPlaying(true)
  }

  const handleSaveConfig = (config: AnalysisFieldConfig) => {
    setSettings(prev => {
      const next = { ...prev, [kind]: config } as SectorFlowSettings
      saveSettings(next)
      return next
    })
    setSelectedKey(null)
  }

  const refreshAll = () => {
    rowsQuery.refetch()
    snapshotQuery.refetch()
    timelineQuery.refetch()
    seriesQuery.refetch()
    marketDatesQuery.refetch()
  }

  const toggleSeriesKey = (key: string) => {
    setSelectedSeriesKeys(prev => (
      prev.includes(key)
        ? prev.filter(item => item !== key)
        : [...prev, key].slice(-10)
    ))
  }

  const pageTitle = viewMode === 'main_flow'
    ? '板块资金'
    : viewMode === 'strength'
      ? '板块强度'
      : '动能气泡'

  if (configsQuery.isLoading) {
    return <div className="flex h-full items-center justify-center"><RefreshCw className="h-5 w-5 animate-spin text-muted" /></div>
  }

  return (
    <>
      <div className="min-w-[960px]">
        <PageHeader
          title={pageTitle}
          subtitle={`${meta.label} · ${displayDate} ${displayTime} · ${viewMode === 'bubble' ? displayStats.length : seriesQuery.data?.sectors.length ?? 0} 个板块`}
          right={
            <div className="flex items-center gap-1">
              <button
                onClick={refreshAll}
                disabled={rowsQuery.isFetching || snapshotQuery.isFetching || timelineQuery.isFetching || seriesQuery.isFetching}
                className="p-1.5 text-muted hover:bg-surface disabled:opacity-50"
                title="刷新"
              >
                <RefreshCw className={cn('h-4 w-4', (rowsQuery.isFetching || snapshotQuery.isFetching || timelineQuery.isFetching || seriesQuery.isFetching) && 'animate-spin')} />
              </button>
              <button
                onClick={() => setShowConfig(true)}
                className="p-1.5 text-muted hover:bg-surface hover:text-accent"
                title="配置数据源"
              >
                <Settings2 className="h-4 w-4" />
              </button>
            </div>
          }
        />

        <div className="min-h-full bg-[radial-gradient(circle_at_12%_0%,rgba(14,165,233,0.10),transparent_28%),radial-gradient(circle_at_88%_8%,rgba(244,63,94,0.08),transparent_30%)] px-6 py-5">
          <div className="mx-auto max-w-[1440px] space-y-4">
          <section className="rounded-lg border border-border bg-surface/80 px-4 py-3">
            <div className="flex flex-wrap items-center gap-3">
              <div className="inline-flex rounded-md border border-border bg-elevated/70 p-0.5">
                {([
                  { key: 'bubble', label: '动能气泡', icon: ChartScatter },
                  { key: 'strength', label: '板块强度', icon: LineChart },
                  { key: 'main_flow', label: '主力流入', icon: Activity },
                ] as const).map(item => {
                  const Icon = item.icon
                  return (
                    <button
                      key={item.key}
                      onClick={() => patchSettings({ viewMode: item.key })}
                      className={cn(
                        'inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs transition-colors',
                        viewMode === item.key ? 'bg-accent text-white' : 'text-secondary hover:bg-surface hover:text-foreground',
                      )}
                    >
                      <Icon className="h-3.5 w-3.5" />
                      {item.label}
                    </button>
                  )
                })}
              </div>

              <div className="inline-flex rounded-md border border-border bg-elevated/70 p-0.5">
                {(['concept', 'industry'] as SectorKind[]).map(k => {
                  const Icon = KIND_META[k].icon
                  return (
                    <button
                      key={k}
                      onClick={() => {
                        patchSettings({ kind: k })
                        setSelectedKey(null)
                      }}
                      className={cn(
                        'inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs transition-colors',
                        kind === k ? 'bg-accent text-white' : 'text-secondary hover:bg-surface hover:text-foreground',
                      )}
                    >
                      <Icon className="h-3.5 w-3.5" />
                      {KIND_META[k].label}
                    </button>
                  )
                })}
              </div>

              <label className="inline-flex h-9 items-center gap-2 rounded-md border border-border bg-elevated/50 px-2.5 text-xs text-secondary">
                <CalendarDays className="h-3.5 w-3.5 text-muted" />
                <select
                  value={selectedDate}
                  onChange={e => {
                    const nextDate = e.target.value || undefined
                    const nextIsToday = !nextDate || nextDate === today
                    patchSettings({ date: nextDate, asOfTs: null, timeMode: nextIsToday && canLive ? 'live' : 'eod' })
                    setIsPlaying(false)
                    setSelectedKey(null)
                  }}
                  className="bg-transparent text-foreground outline-none"
                >
                  {!selectedDate && <option value="">最新</option>}
                  {dates.map(d => <option key={d} value={d}>{d}</option>)}
                </select>
              </label>

              <div className="inline-flex rounded-md border border-border bg-elevated/70 p-0.5">
                <button
                  onClick={() => {
                    patchSettings({ date: today, timeMode: canLive ? 'live' : 'eod', asOfTs: null })
                    setIsPlaying(false)
                  }}
                  disabled={!quoteStatus.data}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40',
                    timeMode === 'live' && isSelectedToday ? 'bg-bull text-white' : 'text-secondary hover:bg-surface hover:text-foreground',
                  )}
                  title={canLive ? '切换到今日实时行情' : realtimeDisabledReason ?? '正在检查实时行情状态'}
                >
                  <Activity className="h-3.5 w-3.5" />
                  实时
                </button>
                <button
                  onClick={startPlayback}
                  disabled={!hasTimeline}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40',
                    timeMode === 'replay' ? 'bg-accent text-white' : 'text-secondary hover:bg-surface hover:text-foreground',
                  )}
                >
                  <Clock3 className="h-3.5 w-3.5" />
                  回放
                </button>
                <button
                  onClick={() => {
                    patchSettings({ timeMode: 'eod', asOfTs: null })
                    setIsPlaying(false)
                  }}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs transition-colors',
                    timeMode === 'eod' ? 'bg-elevated text-foreground shadow-sm' : 'text-secondary hover:bg-surface hover:text-foreground',
                  )}
                >
                  收盘
                </button>
              </div>

              <button
                onClick={() => {
                  if (isPlaying) {
                    setIsPlaying(false)
                  } else {
                    startPlayback()
                  }
                }}
                disabled={!canReplay}
                className={cn(
                  'inline-flex h-9 items-center gap-1.5 rounded-md border border-border px-3 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40',
                  isPlaying ? 'bg-accent text-white' : 'bg-elevated/50 text-foreground hover:bg-surface',
                )}
                title={canReplay ? (isPlaying ? '暂停播放' : '播放回放') : '该日期没有足够的盘中回放点'}
              >
                {isPlaying ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                {isPlaying ? '暂停' : '播放'}
              </button>

              <div className="min-w-[16rem] flex-1">
                <input
                  type="range"
                  min={0}
                  max={Math.max(0, points.length - 1)}
                  value={selectedPointIndex}
                  disabled={!hasTimeline || timeMode !== 'replay'}
                  onChange={e => {
                    const idx = Number(e.target.value)
                    setIsPlaying(false)
                    patchSettings({ timeMode: 'replay', asOfTs: points[idx] ?? null })
                  }}
                  className="h-2 w-full accent-accent disabled:opacity-30"
                  title={hasTimeline ? formatTs(points[selectedPointIndex]) : '无盘中回放点'}
                />
              </div>

              <div className="inline-flex h-9 items-center gap-2 rounded-md border border-border bg-elevated/50 px-2.5 text-xs text-secondary">
                <Search className="h-3.5 w-3.5 text-muted" />
                <input
                  value={search}
                  onChange={e => {
                    setSearch(e.target.value)
                    setSelectedKey(null)
                  }}
                  placeholder="搜索板块"
                  className="w-28 bg-transparent text-foreground outline-none placeholder:text-muted"
                />
              </div>

              <select
                value={sortMode}
                onChange={e => patchSettings({ sortMode: e.target.value as SortMode })}
                className="h-9 rounded-md border border-border bg-elevated/50 px-2.5 text-xs text-foreground outline-none"
              >
                <option value="heat">按强度</option>
                <option value="avgPct">按涨幅</option>
                <option value="amount">按成交额</option>
                <option value="down">按跌幅</option>
                <option value="count">按成分数</option>
              </select>

              <select
                value={maxItems}
                onChange={e => patchSettings({ maxItems: Number(e.target.value) })}
                className="h-9 rounded-md border border-border bg-elevated/50 px-2.5 text-xs text-foreground outline-none"
                title="气泡数量"
              >
                {[36, 48, 64, 80].map(n => <option key={n} value={n}>前 {n}</option>)}
              </select>
            </div>

            <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
              <span>{replayRangeText}</span>
              {!isSelectedToday && (
                <span className="text-amber-500">当前是历史日期 {selectedDate}，不是实时行情</span>
              )}
              {replayDataText && (
                <span className={cn(
                  ['partial_ticks', 'missing_minute', 'missing_today_ticks'].includes(timelineQuery.data?.backfill_status ?? '')
                    ? 'text-bear'
                    : 'text-bull',
                )}>
                  {replayDataText}
                </span>
              )}
              {timeMode === 'replay' && snapshotQuery.data?.mode === 'eod_fallback' && (
                <span className="text-bear">当前时间点没有 tick 快照，已回退到收盘数据</span>
              )}
              {timeMode === 'live' && !canLive && (
                <span className="text-bear">{realtimeDisabledReason}</span>
              )}
              {usingLastGoodStats && (
                <span className="text-bull">当前帧暂未返回有效行情，沿用上一帧气泡</span>
              )}
            </div>

            <div className="mt-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
              <StatusItem label="模式" value={`${modeLabel(timeMode, snapshotQuery.data?.mode)}${phaseText}`} />
              <StatusItem label="行情" value={marketCountText} />
              <StatusItem label="上涨板块" value={`${breadth.up}`} className="text-bull" />
              <StatusItem label="下跌板块" value={`${breadth.down}`} className="text-bear" />
            </div>
          </section>

          {needsPresetFetch ? (
            <PresetFetchState
              title={`未获取${meta.label}数据`}
              hint={`内置${meta.label}数据源已就绪，获取分类数据后即可生成动能气泡`}
              isLoading={fetchMutation.isPending}
              error={fetchMutation.error}
              onFetch={() => fetchMutation.mutate()}
            />
          ) : !activeConfig ? (
            <EmptyState icon={ModeIcon} title={`暂无${meta.label}数据源`} hint={`请先在扩展数据中配置${meta.label}分类数据`} />
          ) : viewMode !== 'bubble' ? (
            <SectorFlowTrendChart
              data={seriesQuery.data}
              metric={seriesMetric}
              selectedKeys={selectedSeriesKeys}
              onToggle={toggleSeriesKey}
              onSelectAll={setSelectedSeriesKeys}
              isLoading={seriesQuery.isLoading}
              isFetching={seriesQuery.isFetching}
              error={(seriesQuery.error as Error | null) ?? null}
              onRefresh={() => seriesQuery.refetch()}
              search={search}
            />
          ) : displayStats.length > 0 ? (
            <>
              <SectorFlowBubbles
                title={`${meta.title}气泡`}
                items={filteredStats}
                selectedKey={selected?.key ?? null}
                onSelect={setSelectedKey}
                height={540}
                maxItems={maxItems}
                playbackActive={isPlaying && timeMode === 'replay'}
                frameKey={bubbleFrameKey}
              />

              <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_22rem]">
                <section className="rounded-lg border border-border bg-surface/80">
                  <div className="flex items-center justify-between border-b border-border/70 px-4 py-3">
                    <div className="inline-flex items-center gap-2 text-sm font-semibold text-foreground">
                      <ChartScatter className="h-4 w-4 text-accent" />
                      动能排行
                    </div>
                    <span className="text-xs text-muted">{filteredStats.length} 个</span>
                  </div>
                  <div className="max-h-[28rem] overflow-auto">
                    {filteredStats.slice(0, 80).map((stat, idx) => (
                      <button
                        key={stat.key}
                        onClick={() => setSelectedKey(stat.key)}
                        className={cn(
                          'grid w-full grid-cols-[2.5rem_1fr_4rem_5rem_4rem] items-center gap-3 border-b border-border/50 px-4 py-2.5 text-left text-xs last:border-b-0 hover:bg-elevated/50',
                          selected?.key === stat.key && 'bg-accent/10',
                        )}
                      >
                        <span className="font-mono text-muted">{idx + 1}</span>
                        <span className="truncate font-medium text-foreground">{stat.key}</span>
                        <span className={cn('font-mono tabular-nums', priceColorClass(stat.avgPct))}>{fmtPct(stat.avgPct)}</span>
                        <span className="font-mono text-secondary tabular-nums">{fmtBigNum(stat.totalAmount)}</span>
                        <span className="font-mono text-muted tabular-nums">{stat.heatScore.toFixed(0)}</span>
                      </button>
                    ))}
                  </div>
                </section>

                <SectorDetail stat={selected} />
              </div>
            </>
          ) : rowsQuery.isLoading || snapshotQuery.isLoading ? (
            <div className="flex h-72 items-center justify-center rounded-lg border border-border bg-surface text-sm text-muted">
              <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
              正在生成动能气泡
            </div>
          ) : (
            <EmptyState
              icon={ModeIcon}
              title="暂无可用气泡"
              hint={resolved.hint || `${meta.label}成分或行情快照为空`}
            />
          )}
          </div>
        </div>
      </div>

      <AnimatePresence>
        {showConfig && (
          <AnalysisConfigDialog
            currentConfig={fieldConfig}
            onSave={handleSaveConfig}
            onClose={() => setShowConfig(false)}
            showHierarchyLevel={kind === 'industry'}
          />
        )}
      </AnimatePresence>
    </>
  )
}

function StatusItem({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="rounded-md bg-elevated/50 px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cn('mt-0.5 truncate font-mono text-sm text-foreground tabular-nums', className)}>{value}</div>
    </div>
  )
}

function SectorDetail({ stat }: { stat: SectorStat | null }) {
  if (!stat) {
    return (
      <section className="rounded-lg border border-border bg-surface/80 p-5 text-sm text-muted">
        选择一个板块查看成分股。
      </section>
    )
  }
  const leaders = [...stat.stocks]
    .filter(s => s.change_pct != null || s.amount != null)
    .sort((a, b) => (b.change_pct ?? -Infinity) - (a.change_pct ?? -Infinity))
    .slice(0, 12)

  return (
    <section className="rounded-lg border border-border bg-surface/80">
      <div className="border-b border-border/70 px-4 py-3">
        <div className="truncate text-sm font-semibold text-foreground">{stat.key}</div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
          <span className={cn('font-mono tabular-nums', priceColorClass(stat.avgPct))}>{fmtPct(stat.avgPct)}</span>
          <span>成交 {fmtBigNum(stat.totalAmount)}</span>
          <span>成分 {stat.count}</span>
          <span>涨{stat.upCount}/跌{stat.downCount}</span>
        </div>
      </div>
      <div className="px-4 py-3">
        {stat.leader && (
          <div className="mb-3 rounded-md bg-elevated/50 px-3 py-2 text-xs">
            <div className="text-muted">龙头</div>
            <div className="mt-1 flex items-center justify-between gap-2">
              <span className="truncate font-medium text-foreground">{stat.leader.name || stat.leader.symbol}</span>
              <span className={cn('font-mono tabular-nums', priceColorClass(stat.leader.change_pct))}>{fmtPct(stat.leader.change_pct)}</span>
            </div>
          </div>
        )}
        <div className="space-y-1">
          {leaders.map(stock => (
            <div key={stock.symbol} className="grid grid-cols-[1fr_4rem_5rem] items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-elevated/50">
              <div className="min-w-0">
                <div className="truncate text-foreground">{stock.name || stock.symbol}</div>
                <div className="font-mono text-[10px] text-muted">{stock.symbol}</div>
              </div>
              <div className={cn('text-right font-mono tabular-nums', priceColorClass(stock.change_pct))}>{fmtPct(stock.change_pct)}</div>
              <div className="text-right font-mono text-muted tabular-nums">{fmtBigNum(stock.amount)}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
