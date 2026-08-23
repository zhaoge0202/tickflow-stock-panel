import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { FlaskConical, HelpCircle, History, Power, RefreshCw, Search, Settings2 } from 'lucide-react'
import { api, type AbnormalOverview, type AbnormalRow, type AbnormalStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtPrice, fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'

/**
 * 异动监控 — 按交易所异动规则口径 (3日±20%/±30%/±40%, 10日+100%, 30日+200%)
 * 实时计算个股「偏离值/阈值」接近度, 找出处于异动边缘的标的。
 *
 * 计算量可控: 主开关默认关闭, 开启后才发起轮询 (每 60s 一次); 关闭后不再计算,
 * 但保留展示上次计算结果 (含计算时间, 取自 localStorage)。
 * 规则口径通过标题栏「?」展开查看。告警走系统监控体系: 在「监控中心」创建
 * 异动监控规则后由后端持续评估, 统一触发记录/站内通知/飞书·企微推送。
 */

const WINDOW_KEYS = ['3d', '10d', '30d'] as const
type WindowKey = (typeof WINDOW_KEYS)[number]

const WINDOW_LABELS: Record<WindowKey, string> = {
  '3d': '3日偏离',
  '10d': '10日偏离',
  '30d': '30日偏离',
}

const STATUS_META: Record<AbnormalStatus, { label: string; cls: string; bar: string }> = {
  triggered: { label: '已触发', cls: 'bg-danger/15 text-danger', bar: 'bg-danger' },
  edge: { label: '异动边缘', cls: 'bg-warning/15 text-warning', bar: 'bg-warning' },
  watch: { label: '观察', cls: 'bg-elevated text-secondary', bar: 'bg-muted' },
}

const BOARDS = ['主板', '创业板', '科创板', '北交所'] as const

const REFRESH_MS = 60_000

export function AbnormalMoves() {
  // 主开关: 默认关闭, 开启后才轮询计算 (仅控制本页计算, 后台告警由监控规则驱动)
  const [enabled, setEnabled] = useState(() => storage.abnormalEnabled.get(false))
  // 规则口径面板 (标题栏「?」)
  const [rulesOpen, setRulesOpen] = useState(false)
  // 上次计算结果: 开启时每次成功计算都落本地, 关闭后仍展示
  const [lastResult, setLastResult] = useState<AbnormalOverview | null>(
    () => (storage.abnormalLastResult.get(null) as AbnormalOverview | null) ?? null,
  )
  const [windowFilter, setWindowFilter] = useState<'all' | WindowKey>('all')
  const [direction, setDirection] = useState<'both' | 'up' | 'down'>('both')
  const [boardFilter, setBoardFilter] = useState<'all' | (typeof BOARDS)[number]>('all')
  const [minCloseness, setMinCloseness] = useState(0.5)
  const [query, setQuery] = useState('')
  const [watchlistOnly, setWatchlistOnly] = useState(false)
  // 默认过滤 ST/*ST 风险警示股票 (口径与后端 is_st_name 一致: 名称含 ST)
  const [excludeSt, setExcludeSt] = useState(true)
  const [preview, setPreview] = useState<{ symbol: string; name: string } | null>(null)

  const overview = useQuery({
    queryKey: QK.abnormalOverview(minCloseness, 300),
    queryFn: () => api.abnormalOverview(minCloseness, 300),
    enabled, // 关闭时零计算
    refetchInterval: enabled ? REFRESH_MS : false,
  })
  // 自选过滤在关闭 (查看上次结果) 时也可用: 自选列表是轻量接口, 不涉及全市场计算
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: watchlistOnly,
  })

  const toggleEnabled = (v: boolean) => {
    setEnabled(v)
    storage.abnormalEnabled.set(v)
    if (v) {
      overview.refetch()
    }
  }

  const data = overview.data
  useEffect(() => {
    if (!data) return
    setLastResult(data)
    storage.abnormalLastResult.set(data)
  }, [data])

  // 展示数据源: 开启 → 实时结果; 关闭 → 上次计算结果 (可能为空)
  const view = enabled ? data : lastResult
  const stale = !enabled && lastResult != null

  const watchSymbols = useMemo(() => {
    const set = new Set((watchlist.data?.symbols ?? []).map(e => e.symbol))
    return set
  }, [watchlist.data])

  const rows = useMemo(() => {
    let list = view?.rows ?? []
    if (windowFilter !== 'all') {
      list = list.filter(r => {
        const w = r.windows[windowFilter]
        return w != null && w.closeness >= minCloseness
      })
    }
    if (direction !== 'both') {
      list = list.filter(r => {
        const w = windowFilter !== 'all' ? r.windows[windowFilter] : dominantWindow(r)
        const v = w?.value ?? 0
        return direction === 'up' ? v > 0 : v < 0
      })
    }
    if (boardFilter !== 'all') list = list.filter(r => r.board === boardFilter)
    if (watchlistOnly) list = list.filter(r => watchSymbols.has(r.symbol))
    if (excludeSt) list = list.filter(r => !(r.name ?? '').toUpperCase().includes('ST'))
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter(r => `${r.symbol} ${r.name ?? ''}`.toLowerCase().includes(q))
    }
    return list
  }, [view, windowFilter, direction, boardFilter, watchlistOnly, excludeSt, watchSymbols, query, minCloseness])

  const counts = view?.counts
  const updating = overview.isFetching

  return (
    // 整页占满视口: 头部/筛选固定, 只有表格列表区滚动
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0">
        <PageHeader
        title="异动监控"
        subtitle="3日异常波动 / 10日·30日严重异常波动 · 偏离值接近度"
        right={
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-pressed={rulesOpen}
              aria-label="查看异动规则口径"
              title="交易所异动规则口径 (阈值 / 偏离值计算方式)"
              onClick={() => setRulesOpen(v => !v)}
              className={`inline-flex h-7 w-7 items-center justify-center rounded border transition-colors ${
                rulesOpen
                  ? 'border-accent/40 bg-accent/10 text-accent'
                  : 'border-border bg-base text-secondary hover:text-foreground'
              }`}
            >
              <HelpCircle className="h-3.5 w-3.5" />
            </button>
            {enabled && (
              <button
                type="button"
                onClick={() => overview.refetch()}
                className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
                title="立即刷新"
              >
                <RefreshCw className={`h-3 w-3 ${updating ? 'animate-spin' : ''}`} />
                刷新
              </button>
            )}
          <Link
            to="/monitor"
            className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
            title="在监控中心创建「异动监控」规则: 后台持续评估, 触发时统一走触发记录/站内通知/飞书·企微推送, 无需保持本页打开"
          >
            <Settings2 className="h-3 w-3" />
            告警规则
          </Link>
          {/* 主开关: 开启后才开始轮询计算 */}
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              aria-label="启用异动监控计算"
              onClick={() => toggleEnabled(!enabled)}
              className={`inline-flex h-7 items-center gap-2 rounded border px-2.5 text-[11px] font-medium transition-colors ${
                enabled
                  ? 'border-accent/40 bg-accent/12 text-accent'
                  : 'border-border bg-base text-secondary hover:text-foreground'
              }`}
            >
              <Power className="h-3 w-3" />
              {enabled ? '监控中 · 每60秒计算' : '开启监控'}
            </button>
          </div>
        }
      />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 px-5 py-4">

      {/* 规则口径面板 (标题栏「?」展开) */}
      {rulesOpen && (
        <div className="shrink-0 rounded-card border border-border bg-surface p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {ruleChips()}
          </div>
          <p className="mt-2.5 border-t border-border/60 pt-2 text-[10px] leading-relaxed text-muted">
            口径说明: 偏离值 = 个股 N 日累计涨跌幅 − 对应指数同期涨跌幅 (沪: 上证A指/上证指数,
            深: 深证A指/深证成指, 北: 北证50)。阈值为交易所异常波动披露标准的近似值, 仅供风险提示,
            不构成监管认定。每只股票在 3日/10日/30日 三档各算一个接近度 (|偏离值| ÷ 该档阈值,
            阈值随板块不同; 2026-07-06 起主板风险警示股票与普通股票同口径), 表格「接近度」列与状态取三档中的最高值,
            来源窗口的偏离值颜色加重显示、其余窗口淡化; ≥100% 已触发、≥70% 边缘、≥50% 观察。
            偏离列亦可在自选/选股的「异动」列组中启用, 并可作为监控规则与自定义信号的阈值字段。
          </p>
        </div>
      )}

      {/* 未开启且无历史结果: 说明 + 开启入口 (有上次结果时直接展示数据, 见下方 stale 横幅) */}
      {!enabled && !stale ? (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <div className="m-auto rounded-card border border-border bg-surface p-8 text-center">
            <FlaskConical className="mx-auto h-8 w-8 text-muted/50" />
            <div className="mt-3 text-sm font-medium text-foreground">监控未开启</div>
            <p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-muted">
              开启后按交易所异动规则实时计算全市场个股的涨跌幅偏离值 (个股 N 日累计涨跌 −
              对应指数同期), 找出接近触发「异常波动 / 严重异常波动」的标的。
              计算量较大, 默认关闭; 每次计算的结果会保留, 关闭后仍可查看 (不再实时更新)。
            </p>
            <p className="mx-auto mt-2 max-w-lg text-[11px] leading-relaxed text-muted/80">
              需要告警推送时, 在<Link to="/monitor?new=abnormal" className="text-accent hover:underline">监控中心</Link>
              新建「异动监控」规则 —— 后台持续评估, 触发时统一走触发记录 / 站内通知 / 飞书·企微推送,
              与本页开关互不影响。
            </p>
            <button
              type="button"
              onClick={() => toggleEnabled(true)}
              className="mt-5 inline-flex h-9 items-center gap-2 rounded-btn bg-accent px-4 text-xs font-medium text-base"
            >
              <Power className="h-4 w-4" />
              开启监控
            </button>
          </div>
        </div>
      ) : (
        <>
          {/* 关闭后展示上次计算结果 */}
          {stale && (
            <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-card border border-warning/25 bg-warning/5 px-3 py-2">
              <History className="h-3.5 w-3.5 shrink-0 text-warning" />
              <span className="text-[11px] font-medium text-warning">已暂停计算 · 展示上次结果</span>
              <span className="text-[11px] text-secondary">
                上次计算 {fmtCalcTime(lastResult.asof)} · 数据截至 {lastResult.cache_date ?? '—'}
                {lastResult.includes_today ? ' (含今日收盘)' : ''}
              </span>
              <button
                type="button"
                onClick={() => toggleEnabled(true)}
                className="ml-auto inline-flex h-7 shrink-0 items-center gap-1 rounded border border-accent/40 bg-accent/10 px-2.5 text-[11px] font-medium text-accent transition-colors hover:bg-accent/15"
              >
                <Power className="h-3 w-3" />
                开启实时计算
              </button>
            </div>
          )}

          {/* 统计 + 筛选 */}
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <StatusChip label="已触发" count={counts?.triggered} tone="danger" />
            <StatusChip label="异动边缘" count={counts?.edge} tone="warning" />
            <StatusChip label="观察" count={counts?.watch} tone="muted" />
            {enabled && (
              <span className="text-[10px] text-muted">
                数据截至 {data?.cache_date ?? '—'}
                {data?.includes_today ? ' (含今日收盘)' : ' · 已叠加今日实时涨跌'}
                {data ? ` · 基准指数今日 ${(data.bench_rt_pct * 100).toFixed(2)}%` : ''}
              </span>
            )}
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <SegmentedControl
              value={windowFilter}
              onChange={v => setWindowFilter(v)}
              options={[
                { value: 'all', label: '全部窗口' },
                ...WINDOW_KEYS.map(w => ({ value: w, label: WINDOW_LABELS[w] })),
              ]}
            />
            <SegmentedControl
              value={direction}
              onChange={v => setDirection(v)}
              options={[
                { value: 'both', label: '双向' },
                { value: 'up', label: '正向' },
                { value: 'down', label: '负向' },
              ]}
            />
            <SegmentedControl
              value={boardFilter}
              onChange={v => setBoardFilter(v)}
              options={[
                { value: 'all' as const, label: '全板块' },
                ...BOARDS.map(b => ({ value: b, label: b })),
              ]}
            />
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="只看自选列表中的标的">
              <input
                type="checkbox"
                checked={watchlistOnly}
                onChange={e => setWatchlistOnly(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              只看自选
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="过滤 ST/*ST 风险警示股票">
              <input
                type="checkbox"
                checked={excludeSt}
                onChange={e => setExcludeSt(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              过滤ST
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="接近度下限 (|偏离|/阈值)">
              接近度 ≥ {(minCloseness * 100).toFixed(0)}%
              <input
                type="range"
                min={30}
                max={100}
                step={5}
                value={minCloseness * 100}
                onChange={e => setMinCloseness(Number(e.target.value) / 100)}
                className="h-1 w-24 accent-accent"
              />
            </label>
            <div className="relative ml-auto">
              <Search className="absolute left-2 top-1.5 h-3.5 w-3.5 text-muted" />
              <input
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="搜索代码/名称"
                className="h-7 w-40 rounded border border-border bg-base pl-7 pr-2 text-[11px] text-foreground"
              />
            </div>
          </div>

          {/* 主表: 剩余空间内滚动 (页面本身不滚动) */}
          <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border bg-surface">
            <table className="w-full min-w-[860px] text-xs">
              <thead className="sticky top-0 z-10 bg-surface">
                <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted">
                  <th className="w-10 px-2 py-2 text-right">#</th>
                  <th className="px-2 py-2 text-left">代码 / 名称</th>
                  <th className="px-2 py-2 text-right">现价</th>
                  <th className="px-2 py-2 text-right">今日</th>
                  {WINDOW_KEYS.map(w => (
                    <th key={w} className="px-2 py-2 text-right">
                      {WINDOW_LABELS[w]}
                      <span className="ml-1 normal-case text-muted/60">(阈值)</span>
                    </th>
                  ))}
                  <th
                    className="w-36 px-2 py-2 text-left"
                    title="取 3日/10日/30日 三档中最高的 |偏离值|÷对应档阈值; ≥100% 已触发, ≥70% 边缘, ≥50% 观察"
                  >
                    接近度
                    <span className="ml-1 normal-case text-muted/60">(最高档)</span>
                  </th>
                  <th className="px-2 py-2 text-center">状态</th>
                </tr>
              </thead>
              <tbody>
                {overview.isLoading ? (
                  <tr>
                    <td colSpan={9} className="px-3 py-10 text-center text-muted">
                      正在计算全市场偏离值…
                    </td>
                  </tr>
                ) : rows.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="px-3 py-10 text-center text-muted">
                      {view ? '当前没有满足条件的标的' : '暂无数据'}
                    </td>
                  </tr>
                ) : (
                  rows.map((r, i) => (
                    <AbnormalRowView
                      key={r.symbol}
                      row={r}
                      rank={i + 1}
                      onPreview={() => setPreview({ symbol: r.symbol, name: r.name ?? r.symbol })}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      </div>
      {preview && (
        <StockPreviewDialog
          symbol={preview.symbol}
          name={preview.name}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  )

  function ruleChips() {
    return (view?.rules ?? FALLBACK_RULES).map((rule, i) => {
      // 对称窗口 (3日) 显示 ±X%; 严重异动窗口正负阈值不同, 显示 +X%/−Y%
      const thr = WINDOW_KEYS.map(w => {
        const t = rule.thresholds[w]
        const s = t.up === t.down ? `±${fmtThreshold(t.up)}` : `+${fmtThreshold(t.up)}/−${fmtThreshold(t.down)}`
        return `${w.replace('d', '日')}${s}`
      }).join(' / ')
      return (
        <div key={i} className="rounded border border-border bg-base px-2.5 py-2">
          <div className="text-[11px] font-medium text-foreground">
            {rule.board}
            {rule.st && <span className="ml-1 text-danger">ST</span>}
          </div>
          <div className="mt-0.5 font-mono text-[10px] text-muted">{thr}</div>
        </div>
      )
    })
  }
}

/** 上次计算时间 (服务端 asof 秒级时间戳 → 本地日期时间) */
function fmtCalcTime(asofSec: number): string {
  const d = new Date(asofSec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtThreshold(v: number | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(0)}%`
}

/** 全窗口里接近度最高的窗口 */
function dominantWindow(r: AbnormalRow): { key: WindowKey; value: number; threshold: number; closeness: number } | undefined {
  let best: { key: WindowKey; value: number; threshold: number; closeness: number } | undefined
  for (const w of WINDOW_KEYS) {
    const info = r.windows[w]
    if (info && (!best || info.closeness > best.closeness)) best = { key: w, ...info }
  }
  return best
}

function AbnormalRowView({ row, rank, onPreview }: {
  row: AbnormalRow
  rank: number
  onPreview: () => void
}) {
  const board = boardTag(row.symbol)
  const dominant = dominantWindow(row)
  const meta = STATUS_META[row.status]
  return (
    <tr className="group border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/50">
      <td className="px-2 py-1.5 text-right font-mono text-[10px] text-muted/70">{rank}</td>
      <td className="px-2 py-1.5">
        {/* 仅代码/名称可点击打开详情 (与自选列表一致), 其余单元格不可点 */}
        <button
          type="button"
          onClick={onPreview}
          title="查看个股详情"
          className="flex min-w-0 items-center gap-1.5 text-left"
        >
          <span className="shrink-0 font-mono text-xs text-foreground group-hover:text-accent transition-colors duration-150">{row.symbol}</span>
          <span className="min-w-0 max-w-40 truncate text-xs text-secondary group-hover:text-foreground transition-colors duration-150">{row.name ?? '—'}</span>
          {board && (
            <span className={`shrink-0 rounded px-1 text-[9px] font-bold leading-tight border ${board.color}`}>
              {board.label}
            </span>
          )}
          {row.st && (
            <span className="shrink-0 rounded border border-danger/30 bg-danger/10 px-1 text-[9px] font-bold text-danger">
              ST
            </span>
          )}
        </button>
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs text-secondary">{fmtPrice(row.close)}</td>
      <td className={`px-2 py-1.5 text-right font-mono text-xs font-medium ${priceColorClass(row.rt_pct)}`}>
        {fmtPct(row.rt_pct)}
      </td>
      {WINDOW_KEYS.map(w => {
        const info = row.windows[w]
        // 接近度取最高档: 来源窗口颜色加重 (加粗), 其余窗口淡化, 以此区分「哪一档」
        const isDominant = dominant?.key === w
        // 后端 threshold 已按偏离方向取对应侧 (严重异动负向阈值更严)
        const sign = info && info.value >= 0 ? '+' : '−'
        return (
          <td key={w} className="px-2 py-1.5 text-right">
            {info ? (
              <span
                className={`font-mono text-xs tabular-nums ${priceColorClass(info.value)} ${isDominant ? 'font-semibold' : 'opacity-45'}`}
                title={`阈值 ${sign}${fmtThreshold(info.threshold)} · 接近度 ${(info.closeness * 100).toFixed(0)}%${isDominant ? ' · 本行接近度来源' : ''}`}
              >
                {fmtPct(info.value)}
                <span className="ml-1 text-[9px] text-muted/60">/{sign}{fmtThreshold(info.threshold)}</span>
              </span>
            ) : (
              <span className="text-muted/40">—</span>
            )}
          </td>
        )
      })}
      <td className="px-2 py-1.5">
        <div
          className="flex items-center gap-1.5"
          title="取 3日/10日/30日 三档中最高的 |偏离值|÷对应档阈值; ≥100% 已触发, ≥70% 边缘, ≥50% 观察"
        >
          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-elevated">
            <div
              className={`h-full rounded-full transition-all ${meta.bar}`}
              style={{ width: `${Math.min(100, (dominant?.closeness ?? 0) * 100)}%}` }}
            />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-secondary">
            {((dominant?.closeness ?? 0) * 100).toFixed(0)}%
          </span>
        </div>
      </td>
      <td className="px-2 py-1.5 text-center">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${meta.cls}`}>{meta.label}</span>
      </td>
    </tr>
  )
}

function StatusChip({ label, count, tone }: { label: string; count?: number; tone: 'danger' | 'warning' | 'muted' }) {
  const toneCls =
    tone === 'danger'
      ? 'border-danger/30 bg-danger/8 text-danger'
      : tone === 'warning'
        ? 'border-warning/30 bg-warning/8 text-warning'
        : 'border-border bg-elevated text-secondary'
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${toneCls}`}>
      <span className="font-mono text-sm font-semibold tabular-nums">{count ?? '—'}</span>
      {label}
    </span>
  )
}

function SegmentedControl<T extends string>({ value, onChange, options }: {
  value: T
  onChange: (v: T) => void
  options: Array<{ value: T; label: string }>
}) {
  return (
    <div className="inline-flex h-7 overflow-hidden rounded border border-border bg-base">
      {options.map(o => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={`px-2.5 text-[11px] transition-colors ${
            value === o.value ? 'bg-accent/10 text-accent' : 'text-muted hover:text-foreground'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** 后端数据未到时的规则表兜底 (与后端 RULES_META 同步维护)
 *  阈值为 {正, 负} 双侧: 3日对称, 严重异动 10日+100%/−50%、30日+200%/−70% (负向更严)
 *  2026-07-06 起主板风险警示(ST)股票与普通股票同标准 (原±15%特别规定已废止) */
const FALLBACK_RULES: Array<{ board: string; st: boolean; thresholds: Record<string, { up: number; down: number }>; note: string }> = [
  { board: '主板', st: false, thresholds: { '3d': { up: 0.2, down: 0.2 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
  { board: '创业板/科创板', st: false, thresholds: { '3d': { up: 0.3, down: 0.3 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
  { board: '北交所', st: false, thresholds: { '3d': { up: 0.4, down: 0.4 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
]
