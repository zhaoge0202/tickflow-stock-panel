import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Clock3,
  Edit3,
  History,
  ListChecks,
  Loader2,
  Play,
  RadioTower,
  ShieldAlert,
  Target,
  XCircle,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { api, type DecisionActionPayload, type DecisionItem, type DecisionStatus, type DecisionSummaryResponse, type ManualPosition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtBigNum, fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'
import { toast } from '@/components/Toast'

const STATUS_LABEL: Record<DecisionStatus, string> = {
  pending: '新提醒',
  waiting: '继续等',
  planned: '准备手动下单',
  manual_done: '已手动处理',
  ignored: '今日忽略',
}

const SIDE_LABEL: Record<string, string> = {
  watch: '观察',
  buy_watch: '机会',
  sell_risk: '持仓风险',
  risk: '风险',
}

const SIDE_STYLE: Record<string, string> = {
  watch: 'border-slate-400/20 bg-slate-400/10 text-slate-300',
  buy_watch: 'border-bull/20 bg-bull/10 text-bull',
  sell_risk: 'border-bear/20 bg-bear/10 text-bear',
  risk: 'border-warning/25 bg-warning/10 text-warning',
}

const GROUPS: Array<{ key: string; label: string; match: (item: DecisionItem) => boolean }> = [
  { key: 'new', label: '新机会', match: item => item.status === 'pending' && item.side === 'buy_watch' },
  { key: 'risk', label: '持仓风险', match: item => item.side === 'sell_risk' || item.side === 'risk' },
  { key: 'waiting', label: '等待确认', match: item => item.status === 'waiting' || item.status === 'planned' },
  { key: 'done', label: '已处理', match: item => item.status === 'manual_done' || item.status === 'ignored' },
]

export function Decision() {
  const [params, setParams] = useSearchParams()
  const mode = params.get('mode') === 'replay' ? 'replay' : 'live'
  const selectedSymbol = params.get('symbol') || ''
  const [positionCreateOpen, setPositionCreateOpen] = useState(false)
  const qc = useQueryClient()
  const queueQ = useQuery({
    queryKey: QK.decisionQueue(),
    queryFn: () => api.decisionQueue(),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
    placeholderData: prev => prev,
  })
  const summaryQ = useQuery({
    queryKey: QK.decisionSummary(),
    queryFn: () => api.decisionSummary(),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
    placeholderData: prev => prev,
  })
  const items = queueQ.data?.items ?? []

  useEffect(() => {
    if (mode === 'live' && !selectedSymbol && items.length > 0) {
      const next = new URLSearchParams(params)
      next.set('symbol', items[0].symbol)
      setParams(next, { replace: true })
    }
  }, [items, mode, params, selectedSymbol, setParams])

  const selected = selectedSymbol || items[0]?.symbol || ''
  const detailQ = useQuery({
    queryKey: QK.decisionItem(selected),
    queryFn: () => api.decisionItem(selected),
    enabled: !!selected,
    refetchInterval: 5000,
  })
  const detailItem = detailQ.data?.symbol === selected ? detailQ.data : undefined
  const detailLoading =
    detailQ.isLoading ||
    (!!selected && detailQ.isFetching && !detailItem) ||
    (!!detailQ.data && detailQ.data.symbol !== selected)

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['decision'] })
    qc.invalidateQueries({ queryKey: QK.manualPositions })
  }
  const setMode = (nextMode: 'live' | 'replay') => {
    const next = new URLSearchParams(params)
    if (nextMode === 'replay') next.set('mode', 'replay')
    else next.delete('mode')
    setParams(next, { replace: true })
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="盘中决策台" subtitle="秒级提醒 · 人工确认 · 手动处理记录" />
      <div className="flex-1 min-h-0 px-5 py-4">
        <div className="mx-auto flex h-full max-w-7xl flex-col gap-4">
          <StatusStrip queue={queueQ.data} summary={summaryQ.data} loading={queueQ.isLoading} summaryLoading={summaryQ.isLoading} />
          <div className="flex items-center justify-between gap-3">
            <div className="inline-flex w-fit rounded-lg border border-border bg-surface p-1">
              <button onClick={() => setMode('live')} className={cn('inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors', mode === 'live' ? 'bg-accent text-white' : 'text-muted hover:bg-elevated hover:text-foreground')}>
                <Target className="h-3.5 w-3.5" />实时队列
              </button>
              <button onClick={() => setMode('replay')} className={cn('inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors', mode === 'replay' ? 'bg-accent text-white' : 'text-muted hover:bg-elevated hover:text-foreground')}>
                <History className="h-3.5 w-3.5" />盘中回放
              </button>
            </div>
            <button
              onClick={() => {
                setMode('live')
                setPositionCreateOpen(true)
              }}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-foreground"
            >
              <ShieldAlert className="h-3.5 w-3.5" />新增持仓
            </button>
          </div>
          {mode === 'replay' ? (
            <ReplayPanel defaultSymbols={items.map(item => item.symbol)} />
          ) : (
            <div className="grid min-h-0 flex-1 grid-cols-[390px_1fr] gap-4">
              <DecisionQueue
                items={items}
                selected={selected}
                loading={queueQ.isLoading}
                onAddPosition={() => setPositionCreateOpen(true)}
                onSelect={(symbol) => {
                  const next = new URLSearchParams(params)
                  next.set('symbol', symbol)
                  next.delete('mode')
                  setParams(next)
                }}
              />
              <DecisionDetail
                item={detailItem}
                loading={detailLoading}
                onChanged={invalidate}
              />
            </div>
          )}
        </div>
      </div>
      {positionCreateOpen && (
        <PositionDialog
          onClose={() => setPositionCreateOpen(false)}
          onSaved={(symbol) => {
            setPositionCreateOpen(false)
            invalidate()
            const next = new URLSearchParams(params)
            next.delete('mode')
            next.set('symbol', symbol)
            setParams(next)
          }}
        />
      )}
    </div>
  )
}

function ReplayPanel({ defaultSymbols }: { defaultSymbols: string[] }) {
  const today = new Date().toLocaleDateString('sv-SE')
  const [tradeDate, setTradeDate] = useState(today)
  const [symbols, setSymbols] = useState(defaultSymbols.slice(0, 8).join(','))
  const [startTime, setStartTime] = useState('09:30')
  const [endTime, setEndTime] = useState('15:00')
  const replayMut = useMutation({
    mutationFn: () => api.intradayReplay({
      date: tradeDate,
      symbols: symbols.split(/[,\s，;；]+/).map(s => s.trim().toUpperCase()).filter(Boolean),
      start_time: startTime || undefined,
      end_time: endTime || undefined,
    }),
  })
  const data = replayMut.data

  return (
    <section className="min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-surface/45">
      <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
        <History className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-semibold text-foreground">盘中回放</h2>
        <span className="ml-auto text-[11px] text-muted">读取历史 quote_ticks, 生成模拟提醒</span>
      </div>
      <div className="grid h-full min-h-0 grid-cols-[360px_1fr] gap-4 p-4">
        <div className="space-y-3">
          <FieldText label="日期" type="date" value={tradeDate} onChange={setTradeDate} />
          <FieldText label="股票池" value={symbols} onChange={setSymbols} placeholder="002491.SZ,300750.SZ" />
          <div className="grid grid-cols-2 gap-2">
            <FieldText label="开始" type="time" value={startTime} onChange={setStartTime} />
            <FieldText label="结束" type="time" value={endTime} onChange={setEndTime} />
          </div>
          <button
            onClick={() => replayMut.mutate()}
            disabled={replayMut.isPending || !tradeDate || !symbols.trim()}
            className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white disabled:opacity-50"
          >
            {replayMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            开始回放
          </button>
          {replayMut.error && <p className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{String((replayMut.error as Error).message)}</p>}
        </div>
        <div className="min-h-0 overflow-auto rounded-lg border border-border/50 bg-base/25 p-3">
          {!data ? (
            <EmptyState icon={History} title="未运行回放" hint="选择日期、股票池和时间范围后开始验证提醒质量。" />
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-2 xl:grid-cols-8">
                <Metric label="任务状态" value={data.status} />
                <Metric label="触发次数" value={String(data.triggered ?? 0)} />
                <Metric label="标的数" value={String(data.symbols?.length ?? 0)} />
                <Metric label="匹配Tick" value={`${data.window_tick_count ?? 0}/${data.tick_count ?? 0}`} />
                <Metric label="数据源" value={replaySourceLabel(data.tick_source)} />
                <Metric label="Tick范围" value={formatReplayRange(data.tick_time_range) || '-'} />
                <Metric label="规则数" value={String(data.rule_count ?? 0)} />
                <Metric label="日期" value={data.date} />
              </div>
              {data.symbols?.length > 0 && (
                <div className="space-y-1 rounded-md border border-border/45 bg-base/25 px-3 py-2 text-[11px] text-muted">
                  <div>
                    实际标的 <span className="ml-1 font-mono text-secondary">{data.symbols.join(', ')}</span>
                    {data.requested_symbols?.join(',') !== data.symbols?.join(',') && (
                      <span className="ml-2 text-muted/70">由 {data.requested_symbols?.join(', ')} 自动规范化</span>
                    )}
                  </div>
                  <div className="font-mono text-muted/80">
                    quote_ticks {data.quote_window_tick_count ?? 0}/{data.quote_tick_count ?? 0}
                    <span className="mx-2 text-muted/40">·</span>
                    tdx逐笔 {data.trade_window_tick_count ?? 0}/{data.trade_tick_count ?? 0}
                    {formatReplayRange(data.window_time_range) && (
                      <>
                        <span className="mx-2 text-muted/40">·</span>
                        窗口 {formatReplayRange(data.window_time_range)}
                      </>
                    )}
                  </div>
                </div>
              )}
              {replayEmptyHint(data) && (
                <div className="rounded-md border border-amber-400/25 bg-amber-400/10 px-3 py-2 text-xs leading-relaxed text-amber-200">
                  {replayEmptyHint(data)}
                </div>
              )}
              <ReplaySummary title="规则表现" rows={data.rule_summary ?? []} />
              <ReplaySummary title="信号表现" rows={data.summary ?? []} />
              <div>
                <h3 className="mb-2 text-xs font-semibold text-foreground">触发明细</h3>
                <div className="space-y-2">
                  {(data.events ?? []).slice(0, 80).map((ev: any, idx: number) => (
                    <div key={`${ev.ts}-${ev.symbol}-${idx}`} className="rounded-md border border-border/50 bg-surface/45 px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-foreground">{ev.symbol}</span>
                        <span className="text-xs text-secondary">{ev.rule_name || ev.signal}</span>
                        <span className="ml-auto font-mono text-[11px] text-muted">{fmtTime(ev.ts)}</span>
                      </div>
                      <p className="mt-1 text-[11px] text-secondary">{ev.reason_text || ev.message}</p>
                      <div className="mt-1 grid grid-cols-4 gap-2 text-[11px]">
                        {(['5m', '15m', '30m', '60m'] as const).map(k => (
                          <span key={k} className={cn('font-mono', priceColorClass(ev.returns?.[k]))}>{k} {fmtPct(ev.returns?.[k])}</span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}

function StatusStrip({ queue, summary, loading, summaryLoading }: {
  queue: any
  summary?: DecisionSummaryResponse
  loading: boolean
  summaryLoading: boolean
}) {
  const q = summary?.quality ?? queue?.quality
  const freshness = q?.quote_freshness ?? 'unknown'
  const total = summary?.total ?? queue?.total
  const pending = summary?.pending ?? queue?.pending
  const statsLoading = summaryLoading && total == null
  return (
    <div className="grid grid-cols-5 gap-3">
      <Stat icon={RadioTower} label="实时源" value={q?.source || 'tdxapi'} tone={freshness === 'live' ? 'ok' : freshness === 'unknown' ? 'muted' : 'warn'} />
      <Stat icon={Clock3} label="数据状态" value={freshnessLabel(freshness)} tone={freshness === 'live' ? 'ok' : 'warn'} />
      <Stat icon={Bell} label="今日提醒" value={statsLoading ? '...' : String(total ?? 0)} />
      <Stat icon={ListChecks} label="未处理" value={statsLoading || loading && pending == null ? '...' : String(pending ?? 0)} tone={(pending ?? 0) > 0 ? 'warn' : 'ok'} />
      <Stat icon={AlertTriangle} label="缺失/延迟" value={`${q?.missing_symbols?.length ?? 0}/${q?.stale_symbols?.length ?? 0}`} tone={(q?.missing_symbols?.length || q?.stale_symbols?.length) ? 'warn' : 'ok'} />
    </div>
  )
}

function Stat({ icon: Icon, label, value, tone = 'muted' }: { icon: any; label: string; value: string; tone?: 'ok' | 'warn' | 'muted' }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border/60 bg-surface/50 px-3 py-2.5">
      <span className={cn(
        'flex h-8 w-8 items-center justify-center rounded-md',
        tone === 'ok' ? 'bg-bull/10 text-bull' : tone === 'warn' ? 'bg-warning/10 text-warning' : 'bg-elevated text-muted',
      )}>
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <div className="text-[10px] text-muted">{label}</div>
        <div className="mt-0.5 truncate text-sm font-semibold text-foreground">{value}</div>
      </div>
    </div>
  )
}

function DecisionQueue({ items, selected, loading, onSelect, onAddPosition }: {
  items: DecisionItem[]
  selected: string
  loading: boolean
  onSelect: (symbol: string) => void
  onAddPosition: () => void
}) {
  const grouped = useMemo(() => {
    const used = new Set<string>()
    const out = GROUPS.map(group => {
      const rows = items.filter(item => !used.has(item.id) && group.match(item))
      rows.forEach(item => used.add(item.id))
      return { ...group, rows }
    })
    const rest = items.filter(item => !used.has(item.id))
    if (rest.length) out.unshift({ key: 'other', label: '其他提醒', match: () => false, rows: rest })
    return out
  }, [items])

  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-surface/45">
      <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
        <Target className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-semibold text-foreground">今日队列</h2>
        <span className="ml-auto rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">{items.length}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {loading ? (
          <div className="grid h-32 place-items-center"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
        ) : items.length === 0 ? (
          <div className="grid h-full place-items-center px-6 py-12 text-center">
            <div className="max-w-sm">
              <Bell className="mx-auto h-10 w-10 text-muted" strokeWidth={1.5} />
              <h2 className="mt-4 text-base font-medium text-foreground">暂无决策项</h2>
              <p className="mt-2 text-sm leading-relaxed text-secondary">监控告警、持仓风险或关键价位触发后会进入这里。</p>
              <button
                onClick={onAddPosition}
                className="mt-4 inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white"
              >
                <ShieldAlert className="h-3.5 w-3.5" />录入持仓
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {grouped.map(group => group.rows.length > 0 && (
              <div key={group.key}>
                <div className="mb-2 flex items-center gap-2 px-1">
                  <span className="text-[11px] font-medium text-secondary">{group.label}</span>
                  <span className="h-px flex-1 bg-border/50" />
                  <span className="text-[10px] text-muted">{group.rows.length}</span>
                </div>
                <div className="space-y-2">
                  {group.rows.map(item => (
                    <QueueCard key={item.id} item={item} active={item.symbol === selected} onClick={() => onSelect(item.symbol)} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function QueueCard({ item, active, onClick }: { item: DecisionItem; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'w-full rounded-lg border px-3 py-2.5 text-left transition-colors',
        active ? 'border-accent/50 bg-accent/10' : 'border-border/55 bg-base/25 hover:border-border hover:bg-elevated/40',
      )}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-foreground">{item.name || item.symbol}</span>
            <span className="shrink-0 font-mono text-[10px] text-muted">{item.symbol}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge className={SIDE_STYLE[item.side] ?? SIDE_STYLE.watch}>{SIDE_LABEL[item.side] ?? item.side}</Badge>
            <Badge>{STATUS_LABEL[item.status]}</Badge>
            <Badge>{freshnessLabel(item.quote_freshness)}</Badge>
          </div>
          <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-secondary">
            {item.reasons[0] || '等待新信号'}
          </p>
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-sm font-semibold text-foreground">{fmtPrice(item.latest_price)}</div>
          <div className={cn('mt-0.5 font-mono text-[11px]', priceColorClass(item.change_pct))}>{fmtPct(item.change_pct)}</div>
          <div className="mt-2 rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">{item.priority}</div>
        </div>
      </div>
    </button>
  )
}

function DecisionDetail({ item, loading, onChanged }: {
  item?: DecisionItem
  loading: boolean
  onChanged: () => void
}) {
  const [positionOpen, setPositionOpen] = useState(false)
  const qc = useQueryClient()
  const actionMut = useMutation({
    mutationFn: ({ symbol, payload }: { symbol: string; payload: DecisionActionPayload }) => api.decisionAction(symbol, payload),
    onSuccess: () => {
      onChanged()
      toast('已记录人工处理状态', 'success')
    },
  })
  const outcomesQ = useQuery({
    queryKey: QK.alertOutcomes(7),
    queryFn: () => api.alertOutcomes({ days: 7 }),
    enabled: !!item?.symbol,
    refetchInterval: 15000,
  })
  const outcomes = (outcomesQ.data?.outcomes ?? []).filter((row: any) => row.symbol === item?.symbol)

  if (loading) {
    return <section className="grid place-items-center rounded-lg border border-border bg-surface/45"><Loader2 className="h-6 w-6 animate-spin text-muted" /></section>
  }
  if (!item) {
    return <section className="rounded-lg border border-border bg-surface/45"><EmptyState icon={Target} title="未选择标的" hint="从左侧队列选择一条提醒查看详情。" /></section>
  }

  const frame = item.signal_frame
  const position = item.position

  const doAction = (payload: DecisionActionPayload) => {
    actionMut.mutate({ symbol: item.symbol, payload })
  }

  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-surface/45">
      <div className="flex items-start justify-between gap-3 border-b border-border/60 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate text-base font-semibold text-foreground">{item.name || item.symbol}</h2>
            <span className="font-mono text-xs text-muted">{item.symbol}</span>
            <Badge className={SIDE_STYLE[item.side] ?? SIDE_STYLE.watch}>{SIDE_LABEL[item.side] ?? item.side}</Badge>
          </div>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-secondary">{frame?.reason_text || item.reasons[0] || '暂无解释'}</p>
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-2xl font-bold text-foreground">{fmtPrice(item.latest_price)}</div>
          <div className={cn('font-mono text-xs', priceColorClass(item.change_pct))}>{fmtPct(item.change_pct)}</div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="grid grid-cols-[1.05fr_0.95fr] gap-4">
          <div className="space-y-4">
            <Panel title="信号解释" icon={RadioTower}>
              <div className="grid grid-cols-3 gap-2">
                <Metric label="成交额" value={fmtBigNum(item.amount)} />
                <Metric label="VWAP 偏离" value={fmtPct(frame?.vwap_distance)} />
                <Metric label="5日放量" value={frame?.volume_ratio == null ? '—' : frame.volume_ratio.toFixed(2)} />
                <Metric label="最近支撑" value={fmtPrice(frame?.nearest_support)} />
                <Metric label="最近压力" value={fmtPrice(frame?.nearest_resistance)} />
                <Metric label="数据源" value={frame?.source || 'tdxapi'} />
              </div>
              <TagList title="命中信号" items={item.signals} />
              <TagList title="风险标签" items={item.risk_flags} danger />
            </Panel>

            <Panel title="分钟/逐笔摘要" icon={History}>
              <div className="grid grid-cols-4 gap-2">
                <Metric label="1m涨跌" value={fmtPct(frame?.ret_1m)} />
                <Metric label="5m涨跌" value={fmtPct(frame?.ret_5m)} />
                <Metric label="15m涨跌" value={fmtPct(frame?.ret_15m)} />
                <Metric label="5m量能" value={frame?.amount_ratio_5m == null ? '—' : `${frame.amount_ratio_5m.toFixed(2)}x`} />
                <Metric label="主动买占比" value={fmtPct(frame?.aggressive_buy_ratio)} />
                <Metric label="逐笔净额" value={fmtBigNum(frame?.tick_net_amount)} />
                <Metric label="大单买入" value={fmtBigNum(frame?.large_buy_amount)} />
                <Metric label="大单卖出" value={fmtBigNum(frame?.large_sell_amount)} />
              </div>
            </Panel>

            <Panel title="手动处理" icon={ListChecks}>
              <div className="grid grid-cols-4 gap-2">
                <ActionButton icon={Clock3} label="继续等" onClick={() => doAction({ action: 'mark_wait', price: item.latest_price ?? undefined })} />
                <ActionButton icon={Target} label="准备手动下单" onClick={() => doAction({ action: 'mark_plan', side: item.side, price: item.latest_price ?? undefined })} />
                <ActionButton icon={CheckCircle2} label="已手动处理" onClick={() => doAction({ action: 'mark_manual_done', side: item.side, price: item.latest_price ?? undefined })} />
                <ActionButton icon={XCircle} label="忽略今日" onClick={() => doAction({ action: 'mark_ignore' })} />
              </div>
            </Panel>

            <Panel title="收益追踪" icon={Target}>
              {outcomes.length === 0 ? (
                <p className="text-xs text-muted">暂无该标的的告警后验结果。</p>
              ) : (
                <div className="space-y-2">
                  {outcomes.slice(0, 4).map((row: any) => (
                    <div key={row.alert_key} className="rounded-md border border-border/50 bg-base/25 px-3 py-2">
                      <div className="truncate text-xs text-secondary">{row.message || row.alert_key}</div>
                      <div className="mt-1 grid grid-cols-3 gap-2 text-[11px]">
                        {(['5m', '15m', '30m', '60m', 'close', 'next_day'] as const).map(k => (
                          <span key={k} className={cn('font-mono', priceColorClass(row.returns?.[k]))}>{k} {fmtPct(row.returns?.[k])}</span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <div className="space-y-4">
            <Panel title="手动持仓" icon={ShieldAlert} action={<button onClick={() => setPositionOpen(true)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted hover:bg-elevated hover:text-foreground"><Edit3 className="h-3 w-3" />修改</button>}>
              {position ? <PositionView position={position} /> : <p className="text-xs text-muted">未记录手动持仓。</p>}
            </Panel>

            <Panel title="今日时间线" icon={Clock3}>
              <Timeline events={item.timeline ?? []} />
            </Panel>
          </div>
        </div>
      </div>

      {positionOpen && (
        <PositionDialog
          symbol={item.symbol}
          position={position}
          latestPrice={item.latest_price ?? undefined}
          onClose={() => setPositionOpen(false)}
          onSaved={() => {
            setPositionOpen(false)
            qc.invalidateQueries({ queryKey: QK.manualPositions })
            onChanged()
          }}
        />
      )}
    </section>
  )
}

function Panel({ title, icon: Icon, children, action }: { title: string; icon: any; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border/55 bg-base/25">
      <div className="flex items-center gap-2 border-b border-border/45 px-3 py-2">
        <Icon className="h-3.5 w-3.5 text-accent" />
        <h3 className="text-xs font-semibold text-foreground">{title}</h3>
        <div className="ml-auto">{action}</div>
      </div>
      <div className="p-3">{children}</div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-elevated/35 px-2 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="mt-1 truncate font-mono text-xs text-foreground">{value}</div>
    </div>
  )
}

function replayEmptyHint(data: any): string {
  if (!data || (data.triggered ?? 0) > 0) return ''
  const tickCount = Number(data.tick_count ?? 0)
  const windowTickCount = Number(data.window_tick_count ?? 0)
  const source = replaySourceLabel(data.tick_source)
  const range = formatReplayRange(data.tick_time_range)
  if (tickCount <= 0) {
    const fallback = data.fallback_error ? `tdx逐笔兜底失败: ${data.fallback_error}` : 'tdx逐笔兜底也没有返回可用数据。'
    return `没有匹配到可回放 tick。裸代码会自动补交易所后缀；若仍为 0，说明该日期没有这个标的的可用行情。${fallback}`
  }
  if (windowTickCount <= 0) {
    return `从 ${source} 匹配到 ${tickCount} 条 tick，但都不在当前回放时间窗口内${range ? `；tick 时间范围 ${range}` : ''}。`
  }
  return `窗口内 ${windowTickCount} 条 ${source} tick 已参与回放，当前 ${data.rule_count ?? 0} 条规则未命中。`
}

function replaySourceLabel(source: string | undefined): string {
  if (source === 'tdxapi_trade_ticks') return 'tdx逐笔'
  if (source === 'quote_ticks') return 'quote_ticks'
  return source || '-'
}

function formatReplayRange(range: any): string {
  if (!range?.start || !range?.end) return ''
  return `${formatReplayTime(range.start)}~${formatReplayTime(range.end)}`
}

function formatReplayTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleTimeString('zh-CN', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function FieldText({ label, value, onChange, type = 'text', placeholder }: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
}) {
  return (
    <label className="block">
      <span className="text-[11px] text-muted">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        className="mt-1 h-9 w-full rounded-md border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent/50"
      />
    </label>
  )
}

function ReplaySummary({ title, rows }: { title: string; rows: any[] }) {
  if (!rows.length) return null
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold text-foreground">{title}</h3>
      <div className="grid grid-cols-3 gap-2">
        {rows.slice(0, 6).map(row => (
          <div key={row.key} className="rounded-md border border-border/50 bg-surface/45 px-3 py-2">
            <div className="truncate text-xs text-foreground">{row.key}</div>
            <div className="mt-1 flex items-center justify-between text-[11px]">
              <span className="text-muted">{row.count} 次</span>
              <span className={cn('font-mono', priceColorClass(row.avg_15m))}>15m {fmtPct(row.avg_15m)}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function TagList({ title, items, danger }: { title: string; items: string[]; danger?: boolean }) {
  if (!items.length) return null
  return (
    <div className="mt-3">
      <div className="mb-1 text-[10px] text-muted">{title}</div>
      <div className="flex flex-wrap gap-1.5">
        {items.map(item => (
          <Badge key={item} className={danger ? 'border-warning/25 bg-warning/10 text-warning' : 'border-accent/25 bg-accent/10 text-accent'}>{item}</Badge>
        ))}
      </div>
    </div>
  )
}

function ActionButton({ icon: Icon, label, onClick }: { icon: any; label: string; onClick: () => void }) {
  return (
    <button onClick={onClick} className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border/60 bg-surface px-2 py-2 text-xs text-foreground transition-colors hover:border-accent/40 hover:text-accent">
      <Icon className="h-3.5 w-3.5" />
      <span className="truncate">{label}</span>
    </button>
  )
}

function PositionView({ position }: { position: ManualPosition }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Metric label="持仓数量" value={fmtPrice(position.shares, 0)} />
        <Metric label="成本价" value={fmtPrice(position.cost_price)} />
        <Metric label="止损价" value={fmtPrice(position.stop_loss_price)} />
        <Metric label="目标价" value={fmtPrice(position.take_profit_price)} />
        <Metric label="浮盈亏" value={`${fmtPrice(position.unrealized_pnl)} / ${fmtPct(position.unrealized_pnl_pct)}`} />
        <Metric label="单票风险" value={fmtPrice(position.risk_amount)} />
      </div>
      <div className={cn('rounded-md px-3 py-2 text-xs', position.risk_level === 'critical' ? 'bg-bear/10 text-bear' : position.risk_level === 'warn' ? 'bg-warning/10 text-warning' : 'bg-elevated text-secondary')}>
        {position.position_action_hint || '持仓正常,无动作'}
      </div>
      {position.note && <p className="text-xs leading-relaxed text-muted">{position.note}</p>}
    </div>
  )
}

function Timeline({ events }: { events: any[] }) {
  if (!events.length) return <p className="text-xs text-muted">暂无今日事件。</p>
  return (
    <div className="space-y-2">
      {events.map((ev, idx) => (
        <div key={`${ev.ts}-${idx}`} className="flex gap-2">
          <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate text-xs text-foreground">{ev.title}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted">{fmtTime(ev.ts)}</span>
            </div>
            {ev.message && <p className="mt-0.5 text-[11px] leading-relaxed text-secondary">{ev.message}</p>}
          </div>
        </div>
      ))}
    </div>
  )
}

function PositionDialog({ symbol, position, latestPrice, onClose, onSaved }: {
  symbol?: string
  position?: ManualPosition | null
  latestPrice?: number
  onClose: () => void
  onSaved: (symbol: string) => void
}) {
  const initialSymbol = normalizePositionSymbol(position?.symbol ?? symbol ?? '')
  const [symbolInput, setSymbolInput] = useState(initialSymbol)
  const [form, setForm] = useState<Partial<ManualPosition>>({
    symbol: initialSymbol,
    shares: position?.shares ?? 0,
    cost_price: position?.cost_price ?? latestPrice ?? 0,
    stop_loss_price: position?.stop_loss_price ?? undefined,
    take_profit_price: position?.take_profit_price ?? undefined,
    target_position_pct: position?.target_position_pct ?? undefined,
    note: position?.note ?? '',
  })
  const normalizedSymbol = normalizePositionSymbol(symbolInput)
  const saveMut = useMutation({
    mutationFn: () => api.manualPositionSave(normalizedSymbol, { ...form, symbol: normalizedSymbol }),
    onSuccess: (data) => {
      toast('手动持仓已保存', 'success')
      onSaved(data.position.symbol)
    },
  })
  const set = (key: keyof ManualPosition, value: string) => {
    setForm(prev => ({ ...prev, [key]: key === 'note' ? value : value === '' ? undefined : Number(value) }))
  }
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/45">
      <div className="w-[420px] rounded-lg border border-border bg-surface shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold text-foreground">手动持仓</h3>
            <p className="mt-0.5 font-mono text-[11px] text-muted">{normalizedSymbol || '输入股票代码'}</p>
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-muted hover:bg-elevated hover:text-foreground"><XCircle className="h-4 w-4" /></button>
        </div>
        <div className="grid grid-cols-2 gap-3 p-4">
          <label className="col-span-2">
            <span className="text-[11px] text-muted">股票代码</span>
            <input
              value={symbolInput}
              disabled={!!symbol}
              placeholder="002491 或 002491.SZ"
              onChange={e => setSymbolInput(e.target.value)}
              className="mt-1 h-8 w-full rounded-md border border-border bg-base px-2 font-mono text-xs text-foreground outline-none focus:border-accent/50 disabled:opacity-70"
            />
          </label>
          <Field label="持仓数量" value={form.shares} onChange={v => set('shares', v)} />
          <Field label="成本价" value={form.cost_price} onChange={v => set('cost_price', v)} />
          <Field label="止损价" value={form.stop_loss_price} onChange={v => set('stop_loss_price', v)} />
          <Field label="目标价" value={form.take_profit_price} onChange={v => set('take_profit_price', v)} />
          <Field label="目标仓位" value={form.target_position_pct} onChange={v => set('target_position_pct', v)} />
          <div />
          <label className="col-span-2">
            <span className="text-[11px] text-muted">备注</span>
            <textarea
              value={form.note ?? ''}
              onChange={e => set('note', e.target.value)}
              className="mt-1 h-20 w-full resize-none rounded-md border border-border bg-base px-2 py-1.5 text-xs text-foreground outline-none focus:border-accent/50"
            />
          </label>
          {saveMut.error && (
            <p className="col-span-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
              {String((saveMut.error as Error).message)}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <button onClick={onClose} className="rounded-md px-3 py-1.5 text-xs text-muted hover:bg-elevated hover:text-foreground">取消</button>
          <button onClick={() => saveMut.mutate()} disabled={saveMut.isPending || !normalizedSymbol} className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">
            {saveMut.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        </div>
      </div>
    </div>
  )
}

function normalizePositionSymbol(value: string): string {
  const text = String(value || '').trim().toUpperCase()
  if (!text) return ''
  if (text.includes('.')) return text
  if (/^(SH|SZ|BJ)\d{6}$/.test(text)) return `${text.slice(2)}.${text.slice(0, 2)}`
  if (/^\d{6}$/.test(text)) {
    if (text.startsWith('5') || text.startsWith('6')) return `${text}.SH`
    if (text.startsWith('0') || text.startsWith('1') || text.startsWith('3')) return `${text}.SZ`
    if (text.startsWith('8') || text.startsWith('43') || text.startsWith('92')) return `${text}.BJ`
  }
  return text
}

function Field({ label, value, onChange }: { label: string; value: any; onChange: (v: string) => void }) {
  return (
    <label>
      <span className="text-[11px] text-muted">{label}</span>
      <input
        type="number"
        value={value ?? ''}
        onChange={e => onChange(e.target.value)}
        className="mt-1 h-8 w-full rounded-md border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent/50"
      />
    </label>
  )
}

function Badge({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn('inline-flex rounded border border-border/50 bg-elevated/40 px-1.5 py-0.5 text-[10px] font-medium text-muted', className)}>{children}</span>
}

function freshnessLabel(v: string) {
  return { live: '实时', stale: '延迟', snapshot: '快照', unknown: '未知' }[v] ?? v
}

function fmtTime(ts?: number) {
  if (!ts) return '--:--'
  const d = new Date(ts)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
