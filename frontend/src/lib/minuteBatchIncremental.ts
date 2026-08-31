import type { QueryClient } from '@tanstack/react-query'
import { api, type MinuteKlineRow } from '@/lib/api'

// 分钟批量分时的增量轮询助手:
// 后端 /api/kline/minute-batch 已支持 since 增量 (只回 >= since 的K, 含形成中
// 动态最后一根)。这里在 queryFn 内读取 react-query 缓存里的上一轮全量序列,
// 以"各 symbol 最后一根的最旧时间"为 since 请求增量, 并按 (symbol, datetime)
// upsert 合并后返回 — 组件层拿到的仍是完整序列, 代码零改动。
// 缓存不存在 (首次/换标的池/换日) 时不带 since, 全量拉取。

type MinuteBatchData = Record<string, MinuteKlineRow[]>

function lastBarTs(data: MinuteBatchData, symbols?: string[]): string | null {
  // 直接取"最旧最后一根"的原始 datetime 字符串 (与服务端行同格式, 同为北京墙钟):
  // 不做任何 Date/ISO 转换 — toISOString 会变成带 Z 的 UTC, 服务端 naive 比较
  // 会 TypeError, 且换算差 8 小时。固定格式字符串的字典序即时间序。
  const scope = symbols ? new Set(symbols) : null
  let min: string | null = null
  for (const [sym, rows] of Object.entries(data)) {
    if (scope && !scope.has(sym)) continue
    const last = rows[rows.length - 1]
    if (!last) continue
    if (min === null || last.datetime < min) min = last.datetime
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
  const since = prev ? lastBarTs(prev, symbols) ?? undefined : undefined
  const resp = await api.klineMinuteBatch(symbols, undefined, preferLocal, since)
  if (!since || !resp.incremental) return { data: resp.data ?? {} }
  return { data: mergeInto(prev ?? {}, resp.data ?? {}) }
}
