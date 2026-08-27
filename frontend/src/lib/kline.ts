/**
 * 日K查询配置 — klineDaily 的唯一权威 options。
 *
 * StockDailyKChart(图表) 与 StockPanel(信息条) 各自 useQuery 共享同一 cache key,
 * React Query 按 key 去重只发一次请求; 邻近预取 prefetchQuery 也复用本配置, 三处不会漂移。
 *
 * placeholderData 内置"仅同 symbol 占位"守卫: 改日期范围/扩展字段时旧数据可暂显(不闪),
 * 切股时不透传上一只股票的数据(不误显示)。
 */
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 分时 tab 多日分时默认周期 (StockPanel 预取与弹窗存储回退共用, 避免魔数两处漂移) */
export const DEFAULT_INTRADAY_DAYS = 10

export function klineDailyQueryOptions(
  symbol: string,
  dateRange: { start: string; end: string },
  extColumns?: string,
) {
  return {
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, extColumns),
    queryFn: () => api.klineDaily(symbol, undefined, dateRange, extColumns),
    // 工厂无 TData 泛型, 参数用 any 以便 useQuery/prefetchQuery 共用
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === symbol ? prev : undefined
    },
  }
}

/**
 * 单日分时查询配置 — 与 klineDailyQueryOptions 同风格的单源 options (date 为空 = 最新日内)。
 *
 * live 透传上游语义: 当日盘中传 true 后端直接实时拉取最新K (不被分钟增量落盘的本地分区拖慢);
 * 历史日期后端自行忽略 live, 所以预取/多日图恒传 true 也不会影响历史读取。
 */
export function klineMinuteQueryOptions(symbol: string, date?: string, live?: boolean) {
  return {
    queryKey: QK.klineMinute(symbol, date ?? ''),
    queryFn: () => api.klineMinute(symbol, date ?? undefined, live),
  }
}

/** 多日分时查询配置 — 分时 tab 的 StockMultiDayIntradayChart 与 邻近预取 共用。
 * 内嵌「仅同 symbol 占位」守卫 (key 结构 ['kline-minute-range', symbol, days], index 1 为 symbol),
 * 与 klineDailyQueryOptions 同源, 调用点不再各自手写。 */
export function klineMinuteRangeQueryOptions(symbol: string, days: number) {
  return {
    queryKey: QK.klineMinuteRange(symbol, days),
    queryFn: () => api.klineMinuteRange(symbol, days),
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === symbol ? prev : undefined
    },
  }
}
