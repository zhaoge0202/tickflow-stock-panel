import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Database, Loader2, RefreshCw } from 'lucide-react'
import { api, type TradeTickPersistStatus, type TradeTickRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtBigNum, fmtPrice } from '@/lib/format'

interface Props {
  symbol: string
  date: string
}

const LIMIT = 300

function todayLocalISO() {
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

export function StockTradeTicksPanel({ symbol, date }: Props) {
  const qc = useQueryClient()
  const persistKeyRef = useRef('')
  const isToday = date === todayLocalISO()
  const queryKey = useMemo(
    () => QK.tradeTicks(symbol, date, 'auto', 'recent', LIMIT, 'desc'),
    [date, symbol],
  )

  const ticks = useQuery({
    queryKey,
    queryFn: () => api.tradeTicks(
      symbol,
      date,
      'auto',
      'recent',
      LIMIT,
      'desc',
      isToday ? Date.now() : undefined,
    ),
    enabled: !!symbol && !!date,
    staleTime: 0,
    refetchInterval: isToday ? 3000 : false,
    refetchIntervalInBackground: true,
  })

  const persistStatus = useQuery({
    queryKey: QK.tradeTickPersistStatus(symbol, date),
    queryFn: () => api.tradeTickPersistStatus(symbol, date),
    enabled: !!symbol && !!date,
    retry: false,
    refetchInterval: (q) => {
      const status = q.state.data?.status
      return status === 'queued' || status === 'running' ? 3000 : false
    },
  })
  const refetchTicks = ticks.refetch
  const refetchPersistStatus = persistStatus.refetch

  const autoPersist = useMutation({
    mutationFn: () => api.tradeTicksPersist(symbol, date, false),
    onSuccess: () => {
      void persistStatus.refetch()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.tradeTickPersistStatus(symbol, date) })
      qc.invalidateQueries({ queryKey })
    },
  })

  const persist = useMutation({
    mutationFn: (force: boolean) => api.tradeTicksPersist(symbol, date, force),
    onSuccess: () => {
      void persistStatus.refetch()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.tradeTickPersistStatus(symbol, date) })
      qc.invalidateQueries({ queryKey })
    },
  })

  useEffect(() => {
    if (!symbol || !date) return
    const key = `${symbol}|${date}`
    if (persistKeyRef.current === key) return
    persistKeyRef.current = key
    autoPersist.mutate()
  }, [symbol, date])

  const refreshTicks = useCallback(async () => {
    await refetchTicks({ cancelRefetch: true })
    await refetchPersistStatus()
  }, [refetchPersistStatus, refetchTicks])

  const rows = useMemo(() => ticks.data?.rows ?? [], [ticks.data?.rows])
  const mysqlRows = persistStatus.data?.mysql?.rows ?? 0
  const statusError = persistStatus.error instanceof Error ? persistStatus.error.message : null
  const requestPersisting = persist.isPending && !persistStatus.isError
  const persistHint = getPersistHint(persistStatus.data, statusError)

  return (
    <section className="mt-3 border-t border-border pt-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-foreground">分笔成交</span>
          <span className="rounded bg-muted/10 px-1.5 py-0.5 font-mono text-[10px] text-muted">{date}</span>
          <span className="font-mono text-[10px] text-muted">{rows.length}</span>
          {ticks.data?.source && (
            <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">{ticks.data.source}</span>
          )}
          {ticks.data?.time_precision === 'minute' && (
            <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">分钟精度</span>
          )}
          {mysqlRows > 0 && (
            <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">已存 {mysqlRows}</span>
          )}
          {persistHint && (
            <span
              className={`max-w-[420px] truncate text-[10px] ${
                persistHint.level === 'error' ? 'text-danger' : 'text-warning'
              }`}
            >
              {persistHint.text}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => persist.mutate(true)}
            disabled={requestPersisting}
            className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-elevated px-2 text-[11px] text-secondary hover:text-foreground disabled:opacity-50"
            title="保存"
          >
            {requestPersisting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Database className="h-3 w-3" />}
            {requestPersisting ? '保存中' : '保存'}
          </button>
          <button
            onClick={() => { void refreshTicks() }}
            disabled={ticks.isLoading}
            className="inline-flex h-7 w-7 items-center justify-center rounded-btn border border-border bg-elevated text-secondary hover:text-foreground disabled:opacity-50"
            title="刷新"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${ticks.isFetching ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      <div className="h-[220px] overflow-auto rounded-btn border border-border/60">
        <table className="w-full table-fixed text-xs">
          <thead className="sticky top-0 z-10 bg-base">
            <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted">
              <th className="w-[86px] px-2 py-2 text-left font-medium">时间/序号</th>
              <th className="w-[56px] px-2 py-2 text-left font-medium">方向</th>
              <th className="w-[74px] px-2 py-2 text-right font-medium">价格</th>
              <th className="w-[78px] px-2 py-2 text-right font-medium">成交量</th>
              <th className="w-[92px] px-2 py-2 text-right font-medium">成交额</th>
              <th className="w-[54px] px-2 py-2 text-right font-medium">单数</th>
            </tr>
          </thead>
          <tbody>
            {ticks.isLoading && (
              <tr>
                <td colSpan={6} className="h-24 text-center text-xs text-muted">加载中…</td>
              </tr>
            )}
            {!ticks.isLoading && rows.length === 0 && (
              <tr>
                <td colSpan={6} className="h-24 text-center text-xs text-muted">暂无成交明细</td>
              </tr>
            )}
            {rows.map((row) => (
              <TradeTickRowView key={`${row.seq_in_day}-${row.datetime}`} row={row} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function TradeTickRowView({ row }: { row: TradeTickRow }) {
  const sideClass = row.side === 'buy'
    ? 'text-bull'
    : row.side === 'sell'
      ? 'text-bear'
      : 'text-muted'

  return (
    <tr className="border-b border-border/40 last:border-0 hover:bg-elevated/40">
      <td className="px-2 py-1.5 font-mono text-[11px] text-secondary">{fmtTime(row.datetime, row.seq_in_day)}</td>
      <td className={`px-2 py-1.5 text-[11px] font-medium ${sideClass}`}>{row.side_label}</td>
      <td className="px-2 py-1.5 text-right font-mono text-foreground">{fmtPrice(row.price)}</td>
      <td className="px-2 py-1.5 text-right font-mono text-secondary">{fmtBigNum(row.volume)}</td>
      <td className="px-2 py-1.5 text-right font-mono text-secondary">{fmtBigNum(row.amount)}</td>
      <td className="px-2 py-1.5 text-right font-mono text-muted">{row.order_count ?? '—'}</td>
    </tr>
  )
}

function fmtTime(value: string, seq: number) {
  const time = value.includes('T') ? value.split('T')[1] : value
  return `${time.slice(0, 5)} #${seq}`
}

function getPersistHint(
  status: TradeTickPersistStatus | undefined,
  statusError: string | null,
): { text: string; level: 'warn' | 'error' } | null {
  if (statusError) return { text: `保存状态查询失败: ${statusError}`, level: 'error' }
  if (!status) return null
  if (status.status === 'queued') {
    const elapsed = status.elapsed_seconds == null ? '' : ` ${status.elapsed_seconds}s`
    return { text: `后台保存排队中${elapsed}`, level: 'warn' }
  }
  if (status.status === 'running') {
    const elapsed = status.elapsed_seconds == null ? '' : ` ${status.elapsed_seconds}s`
    return { text: `后台保存中${elapsed}`, level: 'warn' }
  }
  if (status.status === 'timeout') {
    return { text: status.error || '后台保存超时，请稍后重试', level: 'error' }
  }
  if (status.status === 'failed' && status.error) {
    return { text: status.error, level: 'error' }
  }
  return null
}
