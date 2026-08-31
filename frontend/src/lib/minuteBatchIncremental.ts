import type { QueryClient } from '@tanstack/react-query'
import { api, type MinuteKlineRow } from '@/lib/api'

// 分钟批量分时的增量轮询助手:
// 后端 /api/kline/minute-batch 已支持 since 增量 (只回 >= since 的K, 含形成中
// 动态最后一根)。这里在 queryFn 内读取 react-query 缓存里的上一轮全量序列,
// 以"各 symbol 最后一根的最旧时间"为 since 请求增量, 并按 (symbol, datetime)
// upsert 合并后返回 — 组件层拿到的仍是完整序列, 代码零改动。
// 缓存不存在 (首次/换标的池/换日) 时不带 since, 全量拉取。

type MinuteBatchData = Record<string, MinuteKlineRow[]>

function lastBarTs(data: MinuteBatchData, symbols?: string[]): number | null {
  // since 只按本轮请求的 symbol 取最旧最后一根: 视口感知下不可见 symbol 可能
  // 落后很多分钟, 把它们计入会把整个批量窗口拉大重拉
  const scope = symbols ? new Set(symbols) : null
  let min: number | null = null
  for (const [sym, rows] of Object.entries(data)) {
    if (scope && !scope.has(sym)) continue
    const last = rows[rows.length - 1]
    if (!last) continue
    const t = new Date(last.datetime).getTime()
    if (Number.isFinite(t) && (min === null || t < min)) min = t
  }
  return min
}

function mergeInto(base: MinuteBatchData, patch: MinuteBatchData): MinuteBatchData {
  const merged: MinuteBatchData = { ...base }
  for (const [sym, rows] of Object.entries(patch)) {
    const cache = merged[sym] ?? []
    const byTs = new Map(cache.map(r => [r.datetime, r]))
    for (const r of rows) byTs.set(r.datetime, r)   // 新值覆盖 (动态K定版)
    merged[sym] = Array.from(byTs.values())
      .sort((a, b) => (a.datetime < b.datetime ? -1 : a.datetime > b.datetime ? 1 : 0))
  }
  return merged
}

/** queryFn 用: 读缓存 → 增量请求 → 合并返回 { data: 完整序列 } (保持端点响应形状, 下游零改动) */
export async function fetchMinuteBatchIncremental(
  qc: QueryClient,
  cacheKey: readonly unknown[],
  symbols: string[],
  preferLocal?: boolean,
): Promise<{ data: MinuteBatchData }> {
  const prev = qc.getQueryData<{ data: MinuteBatchData }>(cacheKey)?.data
  const minTs = prev ? lastBarTs(prev, symbols) : null
  const since = minTs !== null ? new Date(minTs).toISOString() : undefined
  const resp = await api.klineMinuteBatch(symbols, undefined, preferLocal, since)
  if (!since || !resp.incremental) return { data: resp.data ?? {} }
  return { data: mergeInto(prev ?? {}, resp.data ?? {}) }
}
