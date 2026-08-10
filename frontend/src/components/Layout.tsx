import { useEffect, useRef, useState, Suspense } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useQuoteStream, useQuoteStreamStatus } from '@/lib/useQuoteStream'
import { ToastContainer } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import {
  useCapabilities,
  useSettings,
  usePreferences,
  useQuoteStatus,
  useVersion,
} from '@/lib/useSharedQueries'
import {
  useToggleRealtimeQuotes,
} from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import { tierRank } from '@/lib/capability-labels'
import {
  Star,
  ScanSearch,
  History,
  FileText,
  Settings,
  Key,
  Database,
  Loader2,
  LayoutDashboard,
  Tags,
  TrendingUp,
  Flame,
  BarChart3,
  Gauge,
  Sparkles,
  Layers3,
  Landmark,
  RadioTower,
  CheckCircle2,
  BookOpenCheck,
  ExternalLink,
  ChevronRight,
  ChevronDown,
  Sun,
  Moon,
  X,
  WifiOff,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react'
import { Logo } from './Logo'
import { api, type IndexQuote } from '@/lib/api'
import { cn } from '@/lib/cn'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'
import { toggleTheme, useTheme } from '@/lib/theme'
import { setCurrentTotal as setAlertTotal, useUnreadAlerts } from '@/lib/monitorBadge'

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色
const BRAND = '#8B5CF6'
const TICKFLOW_REGISTER_URL = 'https://tickflow.org/auth/register?ref=V3KDKGXPEA'

const CORE_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
] as const

type CoreIndex = (typeof CORE_INDEXES)[number]

const nav = [
  { to: '/',                label: '看板',     icon: LayoutDashboard },
  { to: '/watchlist',  label: '自选',   icon: Star },
  { to: '/screener',   label: '策略',   icon: ScanSearch },
  { to: '/backtest',   label: '回测',   icon: History },
  { to: '/stock-analysis',    label: '个股分析', icon: TrendingUp },
  { to: '/limit-ladder', label: '连板梯队', icon: Flame },
  { to: '/concept-analysis', label: '概念分析', icon: Layers3 },
  { to: '/industry-analysis', label: '行业分析', icon: Landmark },
  { to: '/financials', label: '财务分析', icon: FileText },
  { to: '/monitor', label: '监控中心', icon: RadioTower },
  { to: '/regime', label: '市场环境', icon: Gauge },
  { to: '/review',      label: '复盘',   icon: BookOpenCheck },
  { to: '/indices', label: '指数', icon: BarChart3 },
  { to: '/data',       label: '数据',   icon: Database },
] as const

/** 亮/暗主题切换 — 状态存 localStorage, 生效见 lib/theme.ts */
function ThemeToggle() {
  const theme = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      onClick={() => toggleTheme()}
      className="flex items-center justify-center rounded-btn p-2 text-foreground/80 transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground cursor-pointer"
      title={dark ? '切换到亮色模式' : '切换到暗色模式'}
    >
      {dark ? <Sun className="h-4 w-4 shrink-0" /> : <Moon className="h-4 w-4 shrink-0" />}
    </button>
  )
}

function fmtIndexValue(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(2)
}

function fmtIndexPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`
}

function indexPctClass(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return 'text-muted'
  const n = Number(v)
  if (n === 0) return 'text-foreground'
  return n > 0 ? 'text-bull' : 'text-bear'
}

/** 监控中心未读徽标 — 仅在非监控页且有未读时显示。 */
function MonitorBadge({ active }: { active: boolean }) {
  const unread = useUnreadAlerts()
  // 尊重用户设置: 可在菜单设置里关闭数字提示
  const badgeEnabled = (() => {
    try { return localStorage.getItem('monitor_badge_enabled') !== '0' } catch { return true }
  })()
  if (active || unread <= 0 || !badgeEnabled) return null
  return (
    <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[9px] font-bold text-white animate-pulse">
      {unread > 99 ? '99+' : unread}
    </span>
  )
}

function SidebarIndexQuotes({ rows, items }: { rows: IndexQuote[] | undefined; items: CoreIndex[] }) {
  if (items.length === 0) return null
  const quoteBySymbol = new Map((rows ?? []).map(q => [q.symbol, q]))
  return (
    <div className="mt-2 grid grid-cols-2 gap-1.5">
      {items.map(item => {
        const q = quoteBySymbol.get(item.symbol)
        const value = q?.last_price ?? q?.close
        const pct = q?.change_pct
        return (
          <NavLink
            key={item.symbol}
            to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
            className="block rounded bg-elevated/60 px-2 py-1.5 transition-colors hover:bg-elevated"
            title={`${item.name} ${item.symbol}`}
          >
            <div className="flex items-center justify-between gap-1">
              <span className="text-[10px] text-secondary">{item.name}</span>
              <span className={`text-[10px] font-mono ${indexPctClass(pct)}`}>{fmtIndexPct(pct)}</span>
            </div>
            <div className={`mt-0.5 truncate font-mono text-[10px] ${indexPctClass(pct)}`}>
              {fmtIndexValue(value)}
            </div>
          </NavLink>
        )
      })}
    </div>
  )
}

// ===== 档位卡片 =====
function TierBadge({ label, hasKey, providerName, isTickflow }: { label: string; hasKey?: boolean; providerName: string; isTickflow: boolean }) {
  const base = label.split(' ')[0].split('+')[0].toLowerCase()
  const isNone = base === 'none'

  const tierConfig: Record<string, {
    desc: string
    dotStyle: React.CSSProperties
    tagBg: React.CSSProperties
    labelTextStyle: React.CSSProperties
  }> = {
    none: {
      desc: '未配置 Key · 仅历史日K',
      dotStyle: { background: '#52525b' },
      tagBg: { background: 'rgba(113,113,122,0.15)' },
      labelTextStyle: { color: '#71717a' },
    },
    free: {
      desc: '基础日K · 自选实时',
      dotStyle: { background: '#71717a' },
      tagBg: { background: 'rgba(113,113,122,0.3)' },
      labelTextStyle: { color: '#a1a1aa' },
    },
    starter: {
      desc: '批量同步 · 行情池',
      dotStyle: { background: '#3b82f6' },
      tagBg: { background: 'rgba(59,130,246,0.2)' },
      labelTextStyle: { color: '#60a5fa' },
    },
    pro: {
      desc: '分钟K · 实时行情 · 盘口',
      dotStyle: { background: 'linear-gradient(135deg, #a855f7, #7c3aed)' },
      tagBg: { background: 'linear-gradient(135deg, rgba(168,85,247,0.2), rgba(124,58,237,0.15))' },
      labelTextStyle: { background: 'linear-gradient(135deg, #c084fc, #a855f7)', WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' },
    },
    expert: {
      desc: 'WebSocket · 财务数据',
      dotStyle: { background: 'linear-gradient(135deg, #3b82f6, #a855f7, #f59e0b)' },
      tagBg: { background: 'linear-gradient(135deg, rgba(59,130,246,0.2), rgba(168,85,247,0.2), rgba(245,158,11,0.2))' },
      labelTextStyle: { background: 'linear-gradient(135deg, #60a5fa, #c084fc, #fbbf24)', WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' },
    },
  }

  const t = tierConfig[base] || tierConfig.none
  const displayLabel = isNone ? 'None' : (label || 'None')
  const descText = isNone && !hasKey ? '配置 Key 解锁更多能力' : t.desc

  return (
    <NavLink
      to="/settings?tab=data-sources"
      className="group relative flex items-center gap-2 overflow-hidden rounded-md py-1.5 pl-2.5 pr-2 transition-colors duration-150 hover:bg-elevated/70"
      title={`数据源 · ${providerName} — ${descText}`}
    >
      <span
        className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-accent/50 transition-colors group-hover:bg-accent"
        style={base === 'expert' ? { background: 'linear-gradient(180deg, #60a5fa, #c084fc, #fbbf24)' } : undefined}
      />
      <Key className="h-3.5 w-3.5 shrink-0 text-muted group-hover:text-accent transition-colors" />
      <span className="min-w-0 truncate text-[11px] font-medium text-secondary group-hover:text-foreground transition-colors">
        {providerName || '数据源'}
      </span>
      <span
        className="h-1.5 w-1.5 rounded-full shrink-0"
        style={{ ...t.dotStyle, ...(base === 'expert' ? { animation: 'pulse 2s infinite' } : {}) }}
      />
      {isTickflow && (
        <span
          className="ml-auto inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-bold font-mono leading-none shrink-0"
          style={t.tagBg}
        >
          <span className="truncate" style={t.labelTextStyle}>{displayLabel}</span>
        </span>
      )}
    </NavLink>
  )
}

function AIConfigBadge({ configured, model }: { configured?: boolean; model?: string }) {
  const descText = configured ? (model || '已接入模型') : '接入策略生成模型'
  return (
    <NavLink
      to="/settings?tab=ai"
      className="group relative flex items-center gap-2 overflow-hidden rounded-md py-1.5 pl-2.5 pr-2 transition-colors duration-150 hover:bg-elevated/70"
      title={`AI 配置 — ${descText}`}
    >
      <span className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-purple-400/50 transition-colors group-hover:bg-purple-400" />
      <Sparkles className="h-3.5 w-3.5 shrink-0 text-muted group-hover:text-purple-400 transition-colors" />
      {configured ? (
        <span className="truncate text-[11px] font-medium text-secondary group-hover:text-foreground transition-colors">
          {model || '已接入模型'}
        </span>
      ) : (
        <>
          <span className="text-[11px] text-secondary group-hover:text-foreground transition-colors">AI 配置</span>
          <span className="ml-auto text-[11px] font-mono leading-none text-muted">未配置</span>
        </>
      )}
      <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${configured ? 'bg-bear' : 'bg-warning'}`} />
    </NavLink>
  )
}

export function Layout() {
  // ===== 共享 hooks (替代内联 useQuery) =====
  const { data: caps } = useCapabilities()
  const { data: settingsState } = useSettings()
  const { data: versionData } = useVersion()
  const { data: prefs } = usePreferences()
  // 数据源列表 (用于实时行情状态显示当前数据源名称)
  const { data: dataSources } = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })
  // poll=true: 全局唯一开启条件轮询 (非交易时段 60s 兜底, 交易时段靠 SSE)
  const { data: quoteStatus } = useQuoteStatus({ poll: true })
  const { data: analysisMenus } = useQuery({
    queryKey: QK.analysisMenus,
    queryFn: api.analysisMenus,
  })

  // 自选分组 — 仅当用户开启「显示在侧边栏」时拉取
  const groupsInNav = prefs?.watchlist_groups_in_nav ?? false
  const location = useLocation()
  const { data: watchlistGroupsData } = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
    enabled: groupsInNav,
    staleTime: 60_000,
  })
  const watchlistGroups = watchlistGroupsData?.groups ?? []
  // 自选二级菜单展开状态 — 默认当前在自选页时展开
  const [watchlistNavExpanded, setWatchlistNavExpanded] = useState(location.pathname === '/watchlist')

  // 数据同步状态轮询: 有活跃 job 时「数据」菜单项显示转圈
  const { data: pipelineJobs } = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(1),
    refetchInterval: (query) => (query.state.data?.active_id ? 2000 : 15000),
    refetchIntervalInBackground: true,
  })
  const isDataSyncing = !!pipelineJobs?.active_id

  // 数据同步完成的"瞬时反馈": isDataSyncing 从 true→false 时显示绿色对勾,
  // 闪烁约 3 秒后自动消失。
  const [dataSyncJustDone, setDataSyncJustDone] = useState(false)
  const prevSyncingRef = useRef(false)
  useEffect(() => {
    // 仅在"刚结束"(true→false)且非首次挂载时触发
    if (prevSyncingRef.current && !isDataSyncing) {
      setDataSyncJustDone(true)
      const t = setTimeout(() => setDataSyncJustDone(false), 3000)
      prevSyncingRef.current = isDataSyncing
      return () => clearTimeout(t)
    }
    prevSyncingRef.current = isDataSyncing
  }, [isDataSyncing])

  const qc = useQueryClient()
  const navigate = useNavigate()
  const version = versionData?.version
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  // Free 档监控限制提示: 可手动关闭, 不持久化 (刷新后恢复显示)
  const [dismissFreeHint, setDismissFreeHint] = useState(false)
  // 侧边栏收起状态 — 持久化到 localStorage
  const [navCollapsed, setNavCollapsed] = useState(() => {
    try { return localStorage.getItem('tf-nav-collapsed') === '1' } catch { return false }
  })
  const toggleNavCollapsed = () => {
    setNavCollapsed(prev => {
      const next = !prev
      try { localStorage.setItem('tf-nav-collapsed', next ? '1' : '0') } catch {}
      return next
    })
  }
  const indicesPinned = prefs?.indices_nav_pinned ?? true
  const sidebarIndexSymbols = prefs?.sidebar_index_symbols ?? CORE_INDEXES.map(p => p.symbol)
  const sidebarIndexes = CORE_INDEXES.filter(item => sidebarIndexSymbols.includes(item.symbol))
  // 卡片数据：固定显示时也拉取（即使实时行情关闭）
  const showSidebarQuotes = indicesPinned || realtimeEnabled
  const { data: sidebarIndexQuotes } = useQuery({
    queryKey: [...QK.indexQuotes, 'sidebar', sidebarIndexSymbols.join(',')] as const,
    queryFn: () => api.indexQuotes(sidebarIndexes.map(p => p.symbol)),
    enabled: showSidebarQuotes && sidebarIndexes.length > 0,
    placeholderData: (prev) => prev,
  })

  // SSE: 行情更新时自动刷新相关 queries + 告警通知
  useQuoteStream(realtimeEnabled, prefs?.sse_refresh_pages)
  // 实时 SSE 连接状态 — 断开时底部显示提示, 提示可能漏策略告警
  const streamStatus = useQuoteStreamStatus()

  const toggleQuote = useToggleRealtimeQuotes()
  const isRunning = quoteStatus?.running ?? false
  const isTrading = quoteStatus?.is_trading_hours ?? false
  // 管道/数据修正运行期间实时行情被临时暂停 — 此时禁止开启
  const isPaused = quoteStatus?.paused ?? false
  const tier = tierRank(caps?.label ?? '')
  const isNoneTier = tier < 0
  const isWatchlistMode = tier === 0
  const realtimeModeLabel = isWatchlistMode ? '自选股' : '全市场'
  // 当前实时行情数据源名称 (custom 时显示源名, tickflow 时不显示)
  const realtimeProvider = prefs?.realtime_data_provider
  const realtimeProviderName = realtimeProvider && realtimeProvider !== 'tickflow'
    ? (dataSources?.custom?.find(s => s.name === realtimeProvider)?.display_name || realtimeProvider)
    : null

  // 当前主数据源 (用于侧边栏数据源状态卡)
  const activeProvider = prefs?.daily_data_provider || 'tickflow'
  const activeProviderName = activeProvider === 'tickflow'
    ? 'TickFlow'
    : (dataSources?.custom?.find(s => s.name === activeProvider)?.display_name || activeProvider)
  const isCustomActive = activeProvider !== 'tickflow'

  // 轮询触发记录总数 → 更新监控中心徽标 (每 15 秒)
  const alertsTotalQuery = useQuery({
    queryKey: ['alerts-total'],
    queryFn: () => api.alertsList({ days: 7, limit: 1 }),
    refetchInterval: 15000,
    refetchIntervalInBackground: true,
    select: (data) => data.total,
  })
  // 只在拿到真实总数时同步徽标 (避免 data=undefined 时传 0 重置 lastSeen)
  const alertsTotal = alertsTotalQuery.data
  useEffect(() => {
    if (alertsTotal != null) setAlertTotal(alertsTotal)
  }, [alertsTotal])

  // 合并内置页面 + 可见的扩展分析菜单
  type NavItem = { to: string; label: string; icon: typeof Gauge; badge?: string }
  const analysisNav: NavItem[] = (analysisMenus?.items ?? [])
    .filter(m => m.visible)
    .map(m => ({ to: `/analysis/${m.id}`, label: m.label, icon: m.icon === 'tags' ? Tags : BarChart3 }))

  const allNav: NavItem[] = [...nav, ...analysisNav]
  const savedOrder = prefs?.nav_order ?? []

  const navItems = savedOrder.length > 0
    ? (() => {
        const byTo = new Map(allNav.map(n => [n.to, n]))
        const ordered = savedOrder
          .map(id => byTo.get(id) ?? byTo.get(`/analysis/${id}`))
          .filter(Boolean)
        const seen = new Set(ordered.map(n => n!.to))
        return [...ordered as typeof allNav, ...allNav.filter(n => !seen.has(n.to))]
      })()
    : allNav

  const hiddenIds = new Set(prefs?.nav_hidden ?? [])
  const visibleNavItems = navItems.filter(n => !hiddenIds.has(n.to) && !hiddenIds.has(n.to.replace(/^\/analysis\//, '')))

  const handleToggle = async (enabled: boolean) => {
    // 开启时重新校验档位
    if (enabled) {
      const fresh = await qc.fetchQuery({
        queryKey: QK.capabilities,
        queryFn: api.capabilities,
      })
      const freshTier = tierRank(fresh.label ?? '')
      if (freshTier < 0) return
      if (freshTier === 0 && (prefs?.realtime_watchlist_symbols?.length ?? 0) === 0) {
        navigate('/watchlist')
        return
      }
    }
    await toggleQuote.mutateAsync(enabled)
    // 仅在交易时段立即获取一次行情
    if (enabled && isTrading) {
      api.intradayRefresh().catch(() => {})
    }
  }

  return (
    <div
      className="h-screen grid bg-base text-foreground overflow-hidden transition-[grid-template-columns] duration-200 ease-smooth"
      style={{ gridTemplateColumns: navCollapsed ? '3.5rem 1fr' : '14rem 1fr' }}
    >
      <aside className="border-r border-border bg-surface flex flex-col h-full min-h-0 overflow-hidden">
        <div className={cn('border-b border-border shrink-0', navCollapsed ? 'px-2 pt-3 pb-2' : 'px-4 pt-4 pb-3')}>
          {/* Brand block — 收起时只显 logo 居中 */}
          <div className={cn('flex', navCollapsed ? 'flex-col items-center gap-2' : 'items-center gap-2')}>
            <Logo
              size={navCollapsed ? 24 : 26}
              className="shrink-0 drop-shadow-[0_0_8px_rgba(139,92,246,0.4)]"
              style={{ color: BRAND }}
            />
            {!navCollapsed && (
              <div
                className="font-bold text-[11px] uppercase tracking-[0.14em] text-foreground whitespace-nowrap"
                style={{ textShadow: `0 0 10px ${BRAND}44` }}
              >
                Tick Stock Panel
              </div>
            )}
            {/* 收起/展开 按钮 */}
            <button
              onClick={toggleNavCollapsed}
              className={cn(
                'flex items-center rounded-btn text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth',
                navCollapsed ? 'justify-center p-1.5' : 'ml-auto p-1.5',
              )}
              title={navCollapsed ? '展开菜单' : '收起菜单'}
            >
              {navCollapsed
                ? <PanelLeftOpen className="h-3.5 w-3.5 shrink-0" />
                : <PanelLeftClose className="h-3.5 w-3.5 shrink-0" />
              }
            </button>
          </div>

          {/* 状态卡 — 收起时隐藏 */}
          {!navCollapsed && (
            <div className="mt-2.5 space-y-0.5">
              <TierBadge
                label={caps?.label ?? ''}
                hasKey={settingsState?.mode !== 'none'}
                providerName={activeProviderName}
                isTickflow={!isCustomActive}
              />
              <AIConfigBadge
                configured={settingsState?.ai_configured ?? settingsState?.has_ai_key}
                model={settingsState?.ai_model}
              />
            </div>
          )}
        </div>

        <nav className="flex-1 min-h-0 overflow-y-auto px-2 py-3 space-y-0.5">
          {visibleNavItems.map(({ to, label, icon: Icon, badge }) => {
            // 「自选」项 — 开启分组侧栏且未整体收起时, 渲染为可展开父项 + 二级分组
            const isWatchlistExpandable = to === '/watchlist' && groupsInNav && !navCollapsed && watchlistGroups.length > 0
            return (
              <div key={to}>
                {isWatchlistExpandable ? (
                  /* 可展开的自选父项 — 点击切换展开, 不直接跳页 */
                  <button
                    onClick={() => setWatchlistNavExpanded(v => !v)}
                    className={cn(
                      'group relative flex w-full items-center gap-3 rounded-btn px-3 py-2 text-sm transition-all duration-150 ease-smooth',
                      location.pathname === '/watchlist'
                        ? 'bg-elevated text-foreground font-medium'
                        : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                    )}
                  >
                    <span
                      className={cn(
                        'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                        location.pathname === '/watchlist' ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                      )}
                    />
                    <Icon className={cn('h-4 w-4 shrink-0 transition-colors', location.pathname === '/watchlist' ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                    <span className="flex-1 text-left">{label}</span>
                    {watchlistNavExpanded
                      ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
                      : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />
                    }
                  </button>
                ) : (
                  /* 普通菜单项 */
                  <NavLink
                    to={to}
                    title={navCollapsed ? label : undefined}
                    className={({ isActive }) =>
                      cn(
                        'group relative flex items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
                        navCollapsed ? 'justify-center px-0 py-2' : 'gap-3 px-3 py-2',
                        isActive
                          ? 'bg-elevated text-foreground font-medium'
                          : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {/* active 左侧 accent 竖条指示 */}
                        <span
                          className={cn(
                            'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                            isActive ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                          )}
                        />
                        <Icon className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                        {!navCollapsed && <span className="flex-1">{label}</span>}
                        {!navCollapsed && badge && (
                          <span className="ml-auto inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400 shrink-0">
                            {badge}
                          </span>
                        )}
                        {/* 数据同步状态: 同步中转圈, 刚完成显示绿色对勾闪烁 3 秒 */}
                        {to === '/data' && isDataSyncing && !navCollapsed && (
                          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                        )}
                        {to === '/data' && !isDataSyncing && dataSyncJustDone && !navCollapsed && (
                          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bull animate-pulse" />
                        )}
                        {/* 监控中心徽标: 仅非监控页且有未读时显示 */}
                        {to === '/monitor' && !navCollapsed && <MonitorBadge active={isActive} />}
                      </>
                    )}
                  </NavLink>
                )}

                {/* 自选分组二级子菜单 — 展开时显示 */}
                {isWatchlistExpandable && watchlistNavExpanded && (
                  <div className="mt-0.5 space-y-0.5">
                    <NavLink
                      to="/watchlist"
                      className={({ isActive }) => cn(
                        'flex items-center gap-2 rounded-btn py-1.5 pl-9 pr-3 text-[12px] transition-colors duration-150 ease-smooth',
                        isActive && !location.search
                          ? 'text-accent font-medium'
                          : 'text-foreground/60 hover:text-foreground hover:bg-elevated/50',
                      )}
                    >
                      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted" />
                      <span>全部</span>
                    </NavLink>
                    {watchlistGroups.map(group => {
                      const color = resolveWatchlistGroupColor(group.color)
                      const groupPath = `/watchlist?group=${group.id}`
                      const isGroupActive = location.pathname === '/watchlist' && location.search === `?group=${group.id}`
                      return (
                        <NavLink
                          key={group.id}
                          to={groupPath}
                          className={cn(
                            'flex items-center gap-2 rounded-btn py-1.5 pl-9 pr-3 text-[12px] transition-colors duration-150 ease-smooth',
                            isGroupActive
                              ? 'text-accent font-medium'
                              : 'text-foreground/60 hover:text-foreground hover:bg-elevated/50',
                          )}
                        >
                          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${color.dot}`} />
                          <span className="truncate">{group.name}</span>
                        </NavLink>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </nav>

        {/* 全局行情开关 — 收起时只显示状态指示点 */}
        {navCollapsed ? (
          <div className="border-t border-border px-2 py-2.5 shrink-0 flex justify-center">
            <button
              onClick={() => handleToggle(!realtimeEnabled)}
              disabled={toggleQuote.isPending || isPaused}
              title={realtimeEnabled ? (isRunning && isTrading ? '行情运行中 · 点击关闭' : '实时行情已开启') : '实时行情已关闭 · 点击开启'}
              className="flex items-center justify-center rounded-btn p-1.5 transition-colors hover:bg-elevated/70"
            >
              <span className={`inline-block h-2 w-2 rounded-full ${
                realtimeEnabled && isRunning && isTrading
                  ? 'bg-accent animate-pulse'
                  : realtimeEnabled ? 'bg-warning/60' : 'bg-muted'
              }`} />
            </button>
          </div>
        ) : (
        <div className="border-t border-border px-3 py-2.5 shrink-0">
          {isNoneTier && !realtimeProviderName ? (
            <div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-secondary truncate">实时行情</span>
                <span className="text-[10px] text-accent/70 font-medium bg-accent/10 px-1.5 py-0.5 rounded">
                  Free+
                </span>
              </div>
              <div className="mt-1.5 text-[10px] leading-snug text-muted">
                免费注册
                <a
                  href={TICKFLOW_REGISTER_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="mx-1 inline-flex items-baseline gap-0.5 text-accent/80 hover:text-accent hover:underline"
                >
                  TickFlow
                  <ExternalLink className="h-2.5 w-2.5 self-center" />
                </a>
                开启个股监控
              </div>
            </div>
          ) : (
            /* Starter+ — 开关 + 跳转设置 */
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 min-w-0">
                <span className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${
                  realtimeEnabled && isRunning && isTrading
                    ? 'bg-accent animate-pulse'
                    : realtimeEnabled
                      ? 'bg-warning/60'
                      : 'bg-muted'
                }`} />
                <span className="text-xs text-secondary truncate">
                  实时行情 · {realtimeProviderName || realtimeModeLabel}
                </span>
                <button
                  onClick={() => navigate('/settings?tab=monitoring')}
                  className="text-secondary hover:text-foreground transition-colors shrink-0"
                  title="实时监控设置"
                >
                  <Settings className="h-3 w-3" />
                </button>
              </div>
              <button
                onClick={() => handleToggle(!realtimeEnabled)}
                disabled={toggleQuote.isPending || isPaused}
                title={isPaused ? '数据同步运行中，实时行情已临时暂停' : undefined}
                className={`relative inline-flex h-4 w-7 items-center rounded-full shrink-0 transition-colors duration-200 ${
                  realtimeEnabled
                    ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]'
                    : 'bg-elevated'
                } ${toggleQuote.isPending || isPaused ? 'opacity-50' : 'cursor-pointer'}`}
              >
                <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                  realtimeEnabled ? 'translate-x-[14px]' : 'translate-x-0.5'
                }`} />
              </button>
            </div>
          )}

          {/* 状态提示 */}
          {realtimeEnabled && (!isNoneTier || realtimeProviderName) && (
            <div className="mt-1.5 text-[10px] leading-snug space-y-0.5">
              {isWatchlistMode && !dismissFreeHint && !realtimeProviderName && (
                <div className="flex items-start gap-1 text-amber-400/80">
                  <span className="flex-1">监控自选股前 5 只，全市场监控需 Starter+</span>
                  <button
                    onClick={() => setDismissFreeHint(true)}
                    className="text-amber-400/50 hover:text-amber-400 shrink-0 transition-colors"
                    title="关闭提示"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </div>
              )}
              {isPaused ? (
                <div className="text-warning/80">数据同步运行中，实时行情已临时暂停</div>
              ) : isRunning && isTrading ? (
                <div className="text-accent">行情运行中</div>
              ) : realtimeEnabled && !isTrading ? (
                <div className="text-warning/70">非交易时段，将在交易时间自动开启</div>
              ) : null}
            </div>
          )}
          {showSidebarQuotes && !isWatchlistMode && (!isNoneTier || !!realtimeProviderName) && (
            <SidebarIndexQuotes rows={sidebarIndexQuotes?.rows} items={sidebarIndexes} />
          )}
        </div>
        )}

        <div className={cn('border-t border-border py-3 shrink-0', navCollapsed ? 'px-2 flex flex-col items-center gap-1' : 'px-2')}>
          <div className={navCollapsed ? 'flex flex-col items-center gap-1' : 'flex items-center gap-1'}>
            <ThemeToggle />
            <NavLink
              to="/settings"
              title={navCollapsed ? '设置' : undefined}
              className={({ isActive }) =>
                cn(
                  'group relative flex items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
                  navCollapsed ? 'justify-center px-0 py-2' : 'flex-1 gap-3 px-3 py-2',
                  isActive
                    ? 'bg-elevated text-foreground font-medium'
                    : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <span
                    className={cn(
                      'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                      isActive ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                    )}
                  />
                  <Settings className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                  {!navCollapsed && <span>设置</span>}
                  {!navCollapsed && version && (
                    <span className="ml-auto font-mono text-[10px] text-muted/50 select-none shrink-0">
                      {version}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          </div>
        </div>
      </aside>

      <motion.main
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="h-full overflow-auto scrollbar-gutter-stable"
      >
        {streamStatus === 'reconnecting' && (
          <div
            role="status"
            aria-live="polite"
            className="fixed bottom-4 left-1/2 z-[9998] flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-warning/30 bg-warning/10 px-2.5 py-1 text-[11px] font-medium text-warning shadow-lg backdrop-blur-md"
          >
            <WifiOff className="h-3 w-3 shrink-0 animate-pulse" />
            与服务连接已断开 · 正在重连
          </div>
        )}
        <Suspense
          fallback={
            <div className="flex items-center justify-center py-24">
              <Loader2 className="h-5 w-5 animate-spin text-muted" />
            </div>
          }
        >
          <Outlet />
        </Suspense>
      </motion.main>
      <ToastContainer />
      <AlertToastContainer />
      <AiAnalysisHost />
      <AiReportBubble />
      <StockAnalysisHost />
      <StockAnalysisBubble />
    </div>
  )
}
