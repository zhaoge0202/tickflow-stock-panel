/**
 * 共享 mutation hooks — 消除多页面重复的 useMutation 调用。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from '@/components/Toast'
import { api } from './api'
import { QK } from './queryKeys'

/** 切换实时行情 — Layout / Data 共用 */
export function useToggleRealtimeQuotes() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) => api.updateRealtimeQuotes(enabled),
    onSuccess: (data) => {
      qc.setQueryData(QK.preferences, (prev: any) => prev ? {
        ...prev,
        realtime_quotes_enabled: data.realtime_quotes_enabled,
        ...(typeof data.realtime_allowed === 'boolean' ? { realtime_allowed: data.realtime_allowed } : {}),
      } : prev)
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })

      if (data.error === 'watchlist_empty') {
        toast('当前模式需要先在自选页至少添加 1 只股票，实时开关才会生效。', 'error')
        return
      }
      if (data.realtime_quotes_enabled === false && data.realtime_allowed === false) {
        toast('当前档位不支持实时行情，请先切换到支持实时的通道。', 'error')
      }
    },
  })
}

/** 更新行情轮询间隔 — Layout / Data 共用 */
export function useUpdateQuoteInterval() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: number) => api.updateQuoteInterval(v),
    onSuccess: (data) => {
      qc.setQueryData(QK.quoteInterval, data)
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
    },
  })
}

/** 批量添加自选 — Screener / Intraday 共用 */
export function useWatchlistBatchAdd() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (symbols: string[]) => api.watchlistBatchAdd(symbols),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: QK.watchlistEnriched() })
    },
  })
}
