import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookmarkPlus, ChevronRight, Clock, Layers3, ListFilter, ListPlus, Play, Search } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import { WatchlistGroupMenu } from '@/components/WatchlistAddMenu'
import { api, type FactorBatchItem, type FactorColumn } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { FactorBacktest } from './FactorBacktest'
import { factorBatchCandidate } from './researchCandidates'

const formatDate = (value: Date) => value.toISOString().slice(0, 10)
const monthsAgo = (months: number) => {
  const value = new Date()
  value.setMonth(value.getMonth() - months)
  return formatDate(value)
}
const TODAY = formatDate(new Date())
const INPUT_CLS = 'w-full rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs focus:border-accent focus:outline-none'

type View = 'batch' | 'single'
type SortKey = 'ic' | 'ir' | 'return'

function valueOrBottom(value: number | null) {
  return value == null || !Number.isFinite(value) ? Number.NEGATIVE_INFINITY : Math.abs(value)
}

function BatchDiscovery({ onInspect }: { onInspect: (factorName: string) => void }) {
  const queryClient = useQueryClient()
  const initialized = useRef(false)
  const [selected, setSelected] = useState<string[]>([])
  const [symbols, setSymbols] = useState('')
  const [assetType, setAssetType] = useState<'stock' | 'etf'>('stock')
  const [start, setStart] = useState(monthsAgo(3))
  const [end, setEnd] = useState(TODAY)
  const [nGroups, setNGroups] = useState(5)
  const [rebalance, setRebalance] = useState<'daily' | 'weekly' | 'monthly'>('daily')
  const [fees, setFees] = useState('2')
  const [sortKey, setSortKey] = useState<SortKey>('ic')

  const columns = useQuery({
    queryKey: QK.factorColumns,
    queryFn: api.factorColumns,
  })
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 30_000,
  })
  const watchlistEntries = watchlist.data?.symbols ?? []
  const watchlistCounts = useMemo(() => {
    // 多组并存: 一股计入每个所属分组
    const counts: Record<string, number> = { ungrouped: 0 }
    for (const entry of watchlistEntries) {
      const gids = entry.group_ids ?? []
      if (gids.length === 0) counts.ungrouped += 1
      else for (const gid of gids) counts[gid] = (counts[gid] ?? 0) + 1
    }
    return counts
  }, [watchlistEntries])
  useEffect(() => {
    if (initialized.current || !columns.data?.columns.length) return
    initialized.current = true
    setSelected(columns.data.columns.map(column => column.id))
  }, [columns.data])

  const factorGroups = useMemo(() => {
    const groups: Record<string, FactorColumn[]> = {}
    for (const column of columns.data?.columns ?? []) {
      ;(groups[column.group] ??= []).push(column)
    }
    return groups
  }, [columns.data])

  const run = useMutation({
    mutationFn: () => api.factorBatch({
      factor_names: selected,
      symbols: symbols ? symbols.split(',').map(value => value.trim()).filter(Boolean) : null,
      asset_type: assetType,
      start: start || null,
      end: end || null,
      n_groups: nGroups,
      rebalance,
      fees_pct: Number(fees) / 10000,
    }),
  })
  const save = useMutation({
    mutationFn: (item: FactorBatchItem) => {
      if (!run.data) throw new Error('暂无批量结果')
      return api.researchCandidateCreate(factorBatchCandidate(run.data, item))
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.researchCandidates })
      toast('已保存到候选方案', 'success')
    },
    onError: error => toast(`保存失败 · ${String((error as Error).message || error)}`, 'error'),
  })

  const sortedResults = useMemo(() => {
    const values = [...(run.data?.results ?? [])]
    const getter = sortKey === 'ic'
      ? (item: FactorBatchItem) => valueOrBottom(item.ic_mean)
      : sortKey === 'ir'
        ? (item: FactorBatchItem) => valueOrBottom(item.ir)
        : (item: FactorBatchItem) => valueOrBottom(item.long_short_return)
    return values.sort((left, right) => getter(right) - getter(left))
  }, [run.data, sortKey])

  const toggleFactor = (factorName: string) => {
    setSelected(current => current.includes(factorName)
      ? current.filter(name => name !== factorName)
      : [...current, factorName])
  }
  const toggleGroup = (items: FactorColumn[]) => {
    const ids = items.map(item => item.id)
    const allSelected = ids.every(id => selected.includes(id))
    setSelected(current => allSelected
      ? current.filter(id => !ids.includes(id))
      : [...current, ...ids.filter(id => !current.includes(id))]
    )
  }
  const importFromWatchlist = (groupId: string | null) => {
    // 'all'=全部自选; null=未分组; 其他=指定分组 (多组归属时该股计入每个所属分组)
    const entries = groupId === 'all'
      ? watchlistEntries
      : groupId == null
        ? watchlistEntries.filter(entry => !(entry.group_ids?.length))
        : watchlistEntries.filter(entry => !!entry.group_ids?.includes(groupId))
    const current = symbols.split(',').map(value => value.trim()).filter(Boolean)
    setSymbols(Array.from(new Set([...current, ...entries.map(entry => entry.symbol)])).join(','))
  }
  const allColumns = columns.data?.columns ?? []
  const allSelected = allColumns.length > 0 && allColumns.every(item => selected.includes(item.id))

  return (
    <div className="grid h-full min-h-0 grid-cols-1 overflow-hidden rounded-card border border-border bg-surface/80 xl:grid-cols-[18rem_minmax(0,1fr)]">
      <section className="space-y-3 border-b border-border bg-base/25 px-3 py-3 xl:overflow-y-auto xl:border-b-0 xl:border-r">
        <div className="flex items-center justify-between border-b border-border/70 pb-2">
          <div>
            <div className="text-xs font-semibold text-foreground">筛选配置</div>
            <div className="mt-0.5 text-[10px] text-muted">已选 {selected.length} / {allColumns.length}</div>
          </div>
          <button
            type="button"
            onClick={() => setSelected(allSelected ? [] : allColumns.map(item => item.id))}
            className="rounded-btn px-2 py-1 text-[10px] text-accent transition-colors hover:bg-accent/10"
          >
            {allSelected ? '清空' : '全选'}
          </button>
        </div>

        <div className="space-y-2">
          {Object.entries(factorGroups).map(([group, items]) => {
            const groupSelected = items.filter(item => selected.includes(item.id)).length
            return (
              <div key={group} className="border-b border-border/50 pb-2 last:border-b-0">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="text-[11px] font-medium text-secondary">{group}</span>
                  <button
                    type="button"
                    onClick={() => toggleGroup(items)}
                    className="text-[9px] text-muted transition-colors hover:text-accent"
                  >
                    {groupSelected === items.length ? '取消本组' : `选择本组 ${groupSelected}/${items.length}`}
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-x-2 gap-y-1.5">
                  {items.map(item => (
                    <label key={item.id} className="flex min-w-0 cursor-pointer items-center gap-1.5 text-[10px] text-secondary">
                      <input
                        type="checkbox"
                        checked={selected.includes(item.id)}
                        onChange={() => toggleFactor(item.id)}
                        className="h-3 w-3 shrink-0 accent-accent"
                      />
                      <span className="truncate" title={item.desc}>{item.label}</span>
                    </label>
                  ))}
                </div>
              </div>
            )
          })}
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-secondary">资产与范围</label>
          <div className="mb-2 inline-flex h-8 overflow-hidden rounded-btn border border-border">
            {(['stock', 'etf'] as const).map(value => (
              <button
                key={value}
                type="button"
                onClick={() => { setAssetType(value); setSymbols('') }}
                className={`h-full px-3 text-xs font-medium transition-colors ${assetType === value
                  ? 'bg-accent/10 text-accent'
                  : 'text-muted hover:text-foreground'
                }`}
              >
                {value === 'stock' ? '股票' : 'ETF'}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              value={symbols}
              onChange={event => setSymbols(event.target.value)}
              placeholder="留空使用全市场"
              className={`${INPUT_CLS} min-w-0 flex-1 font-mono`}
            />
            <WatchlistGroupMenu
              onSelect={importFromWatchlist}
              disabled={watchlist.isLoading || watchlistEntries.length === 0}
              includeAll
              counts={watchlistCounts}
              total={watchlistEntries.length}
              disableEmpty
              menuLabel="选择自选分组"
              align="right"
              triggerClassName="inline-flex h-8 shrink-0 items-center gap-1 whitespace-nowrap rounded-input border border-border bg-surface px-2 text-[11px] text-secondary transition-colors hover:border-accent/50 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              title="从自选分组加入筛选范围"
              ariaLabel="从自选加入筛选范围"
            >
              <ListPlus className="h-3 w-3" />
              {watchlist.isLoading ? '加载中' : watchlistEntries.length === 0 ? '自选为空' : '从自选加入'}
            </WatchlistGroupMenu>
          </div>
        </div>

        <div className="rounded-btn border border-border bg-surface p-2.5">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="mb-1 block text-[11px] text-secondary">开始</label>
              <DatePicker value={start} onChange={setStart} max={end || undefined} className="w-full" buttonClassName="w-full justify-start" align="left" />
            </div>
            <div>
              <label className="mb-1 block text-[11px] text-secondary">结束</label>
              <DatePicker value={end} onChange={setEnd} min={start || undefined} className="w-full" buttonClassName="w-full justify-start" />
            </div>
          </div>
          <div className="mt-2 flex rounded-input bg-base/60 p-0.5">
            {[3, 6, 12].map(months => (
              <button
                key={months}
                type="button"
                onClick={() => { setStart(monthsAgo(months)); setEnd(TODAY) }}
                className="flex-1 rounded-btn px-2 py-1 text-[10px] text-muted transition-colors hover:bg-elevated hover:text-secondary"
              >
                {months === 12 ? '1年' : `${months}个月`}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">调仓</span>
            <select value={rebalance} onChange={event => setRebalance(event.target.value as typeof rebalance)} className={INPUT_CLS}>
              <option value="daily">日度</option>
              <option value="weekly">周度</option>
              <option value="monthly">月度</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">分组</span>
            <select value={nGroups} onChange={event => setNGroups(Number(event.target.value))} className={INPUT_CLS}>
              <option value={3}>3组</option>
              <option value={5}>5组</option>
              <option value={10}>10组</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">佣金/万</span>
            <input type="number" value={fees} onChange={event => setFees(event.target.value)} className={INPUT_CLS} />
          </label>
        </div>

        <button
          type="button"
          onClick={() => run.mutate()}
          disabled={run.isPending || selected.length === 0}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Play className="h-3.5 w-3.5" />
          {run.isPending ? '筛选中…' : `筛选 ${selected.length} 个因子`}
        </button>
      </section>

      <section className="min-w-0 bg-base/15 xl:overflow-y-auto">
        {run.isPending && (
          <div className="m-3 flex items-center gap-3 rounded-btn border border-accent/30 bg-accent/5 px-3 py-2.5 text-xs text-secondary">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent/25 border-t-accent" />
            正在加载共享数据面板并评估 {selected.length} 个因子
          </div>
        )}
        {run.isError && (
          <div className="m-3 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {String((run.error as Error).message)}
          </div>
        )}
        {run.data?.error && (
          <div className="m-3 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{run.data.error}</div>
        )}
        {!run.data && !run.isPending && (
          <EmptyState icon={Search} title="运行因子筛选" hint="批量结果将按预测能力排序。" />
        )}
        {run.data && !run.data.error && (
          <div>
            <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
              <div>
                <div className="text-sm font-medium text-foreground">筛选结果</div>
                <div className="mt-0.5 flex items-center gap-3 text-[10px] text-muted">
                  <span>{run.data.results.length} 个因子</span>
                  <span>{run.data.n_symbols} 只标的</span>
                  <span>{run.data.n_dates} 个交易日</span>
                  <span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" />{run.data.elapsed_ms.toFixed(0)} ms</span>
                </div>
              </div>
              <label className="ml-auto flex items-center gap-2 text-[11px] text-muted">
                排序
                <select value={sortKey} onChange={event => setSortKey(event.target.value as SortKey)} className="h-8 rounded-input border border-border bg-surface px-2 text-xs text-secondary focus:border-accent focus:outline-none">
                  <option value="ic">|IC|</option>
                  <option value="ir">|IR|</option>
                  <option value="return">|多空收益|</option>
                </select>
              </label>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] text-xs">
                <thead className="sticky top-0 bg-elevated text-left text-[11px] text-secondary">
                  <tr>
                    <th className="w-12 px-3 py-2.5 text-center font-medium">排名</th>
                    <th className="px-3 py-2.5 font-medium">因子</th>
                    <th className="px-3 py-2.5 text-right font-medium">IC 均值</th>
                    <th className="px-3 py-2.5 text-right font-medium">IR</th>
                    <th className="px-3 py-2.5 text-right font-medium">IC 胜率</th>
                    <th className="px-3 py-2.5 text-right font-medium">多空收益</th>
                    <th className="px-3 py-2.5 text-right font-medium">最大回撤</th>
                    <th className="w-24 px-3 py-2.5 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedResults.map((item, index) => (
                    <tr key={item.factor_name} className="border-t border-border/70 transition-colors hover:bg-elevated/40">
                      <td className="px-3 py-3 text-center font-mono text-muted">{index + 1}</td>
                      <td className="px-3 py-3">
                        <div className="font-medium text-foreground">{item.label}</div>
                        <div className="mt-0.5 text-[10px] text-muted">{item.group} · {item.factor_name}</div>
                        {item.error && <div className="mt-1 text-[10px] text-danger">{item.error}</div>}
                      </td>
                      <td className={`px-3 py-3 text-right font-mono ${priceColorClass(item.ic_mean)}`}>{item.ic_mean == null ? '—' : fmtPct(item.ic_mean)}</td>
                      <td className="px-3 py-3 text-right font-mono text-foreground">{item.ir == null ? '—' : item.ir.toFixed(2)}</td>
                      <td className="px-3 py-3 text-right font-mono text-secondary">{item.ic_win_rate == null ? '—' : fmtPct(item.ic_win_rate)}</td>
                      <td className={`px-3 py-3 text-right font-mono ${priceColorClass(item.long_short_return)}`}>{item.long_short_return == null ? '—' : fmtPct(item.long_short_return)}</td>
                      <td className="px-3 py-3 text-right font-mono text-bear">{item.long_short_max_drawdown == null ? '—' : fmtPct(item.long_short_max_drawdown)}</td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-1">
                          <button
                            type="button"
                            onClick={() => save.mutate(item)}
                            disabled={!!item.error || save.isPending}
                            className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-accent/10 hover:text-accent disabled:opacity-40"
                            title="保存候选"
                            aria-label={`保存 ${item.label} 为候选`}
                          >
                            <BookmarkPlus className="h-3.5 w-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => onInspect(item.factor_name)}
                            disabled={!!item.error}
                            className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-40"
                            title="单因子检验"
                            aria-label={`查看 ${item.label} 的单因子检验`}
                          >
                            <ChevronRight className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

export function FactorDiscovery() {
  const [view, setView] = useState<View>('batch')
  const [detailFactor, setDetailFactor] = useState('momentum_20d')
  const inspect = (factorName: string) => {
    setDetailFactor(factorName)
    setView('single')
  }
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex shrink-0 items-center border-b border-border/70 px-1 pb-2">
        <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
          {([
            ['batch', '批量筛选', ListFilter],
            ['single', '单因子检验', Layers3],
          ] as const).map(([value, label, Icon]) => (
            <button
              key={value}
              type="button"
              onClick={() => setView(value)}
              className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors ${view === value
                ? 'bg-accent text-white shadow-sm'
                : 'text-secondary hover:bg-elevated hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        {view === 'batch'
          ? <BatchDiscovery onInspect={inspect} />
          : <FactorBacktest key={detailFactor} initialFactorName={detailFactor} />
        }
      </div>
    </div>
  )
}
