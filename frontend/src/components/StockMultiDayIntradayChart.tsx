import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, Loader2, RefreshCw } from 'lucide-react'
import { api, type MinuteKlineSession } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EChartsMultiDayIntraday } from '@/components/EChartsMultiDayIntraday'

interface Props {
  symbol: string
  days: number
  height?: number
  refetchIntervalMs?: number
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '分钟数据获取失败'
}

export function StockMultiDayIntradayChart({
  symbol,
  days,
  height = 420,
  refetchIntervalMs,
}: Props) {
  const queryClient = useQueryClient()
  const history = useQuery({
    queryKey: QK.klineMinuteRange(symbol, days),
    queryFn: () => api.klineMinuteRange(symbol, days),
    enabled: !!symbol,
  })
  const latest = useQuery({
    queryKey: QK.klineMinute(symbol, ''),
    queryFn: () => api.klineMinute(symbol),
    enabled: !!symbol,
    refetchInterval: refetchIntervalMs,
  })

  const sessions = useMemo(() => {
    const byDate = new Map<string, MinuteKlineSession>()
    for (const session of history.data?.sessions ?? []) byDate.set(session.date, session)

    const latestDate = latest.data?.date
    const latestRows = latest.data?.rows ?? []
    if (latestDate && latestRows.length > 0) {
      const existing = byDate.get(latestDate)
      byDate.set(latestDate, {
        date: latestDate,
        prev_close: latest.data?.prev_close ?? existing?.prev_close ?? null,
        rows: latestRows,
      })
    }

    return Array.from(byDate.values())
      .sort((left, right) => left.date.localeCompare(right.date))
      .slice(-days)
  }, [days, history.data?.sessions, latest.data])

  const syncMinute = useMutation({
    mutationFn: () => api.syncMinuteSingle(symbol, days),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kline-minute-range', symbol] }),
        queryClient.invalidateQueries({ queryKey: ['kline-minute', symbol] }),
      ])
    },
  })

  const loading = sessions.length === 0 && (history.isLoading || latest.isLoading)
  const queryError = sessions.length === 0 ? history.error ?? latest.error : null
  const isIndex = history.data?.asset_type === 'index' || latest.data?.asset_type === 'index'
  const missingDays = Math.max(0, days - sessions.length)
  const showCoverage = sessions.length > 0 && missingDays > 0 && !isIndex
  const chartHeight = Math.max(260, height - (showCoverage ? 32 : 0))

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 text-xs text-muted" style={{ height }}>
        <Loader2 className="h-4 w-4 animate-spin text-accent" />
        正在加载近 {days} 日分时…
      </div>
    )
  }

  if (queryError) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        <span className="text-danger">{errorMessage(queryError)}</span>
        <button
          type="button"
          onClick={() => { void history.refetch(); void latest.refetch() }}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-secondary hover:text-foreground"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新加载
        </button>
      </div>
    )
  }

  if (sessions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        {syncMinute.isPending ? (
          <>
            <Loader2 className="h-5 w-5 animate-spin text-accent" />
            <span className="text-secondary">正在获取近 {days} 日分钟 K…</span>
          </>
        ) : (
          <>
            <span className="text-muted">{isIndex ? '指数暂无分钟数据' : '本地暂无可展示的分钟数据'}</span>
            {!isIndex && (
              <button
                type="button"
                onClick={() => syncMinute.mutate()}
                className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90"
              >
                <Download className="h-3.5 w-3.5" />
                获取近 {days} 日
              </button>
            )}
          </>
        )}
        {syncMinute.isError && <span className="max-w-md text-center text-danger">{errorMessage(syncMinute.error)}</span>}
      </div>
    )
  }

  return (
    <div style={{ height }}>
      {showCoverage && (
        <div className="flex h-8 items-center justify-between gap-3 border-b border-border/60 bg-elevated/40 px-3 text-[11px]">
          <span className="truncate text-muted">当前有 {sessions.length} 个交易日数据，目标 {days} 日</span>
          <button
            type="button"
            disabled={syncMinute.isPending}
            onClick={() => syncMinute.mutate()}
            className="inline-flex shrink-0 items-center gap-1 text-accent hover:text-accent/80 disabled:opacity-60"
          >
            {syncMinute.isPending
              ? <Loader2 className="h-3 w-3 animate-spin" />
              : <Download className="h-3 w-3" />}
            补齐数据
          </button>
        </div>
      )}
      <EChartsMultiDayIntraday sessions={sessions} height={chartHeight} />
      {syncMinute.isError && (
        <div className="px-3 pt-1 text-center text-[11px] text-danger">{errorMessage(syncMinute.error)}</div>
      )}
    </div>
  )
}
