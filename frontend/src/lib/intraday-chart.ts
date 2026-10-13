import { isCryptoAssetType, type MinuteKlineRow } from '@/lib/api'

export type DailySummary = { date: string; open: number | null; high: number; low: number; close: number }

/** 仅汇总实际收到的分钟，不将缺失开盘价或成交额伪装为真实值。 */
export function summarizeMinutes(data: MinuteKlineRow[]): MinuteKlineRow | null {
  if (!data.length) return null
  return {
    datetime: data[0].datetime,
    open: data[0].open,
    high: Math.max(...data.map(row => row.high)),
    low: Math.min(...data.map(row => row.low)),
    close: data[data.length - 1].close,
    volume: data.reduce((total, row) => total + row.volume, 0),
    amount: data.every(row => row.amount != null && Number.isFinite(row.amount))
      ? data.reduce((total, row) => total + row.amount!, 0) : null,
  }
}

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  if (/[Zz]|[+-]\d{2}:\d{2}$/.test(datetime)) {
    const parsed = new Date(datetime)
    if (!Number.isNaN(parsed.getTime())) {
      return `${String(parsed.getHours()).padStart(2, '0')}:${String(parsed.getMinutes()).padStart(2, '0')}`
    }
  }
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

/** 成交量单位换算: A 股/ETF/指数的 volume 是「手」(1 手 = 100 股), 均价线按股数加权;
 *  crypto 上游口径是币/张 (无「手」概念), 再乘 100 会让均价整体错 100 倍且不报错。 */
export function intradayVolumeMultiplier(assetType?: string | null): number {
  return isCryptoAssetType(assetType) ? 1 : 100
}

export function computeIntradayAverage(data: MinuteKlineRow[], assetType?: string | null): (number | null)[] {
  const result: (number | null)[] = []
  const volumeMultiplier = intradayVolumeMultiplier(assetType)
  let amount = 0
  let volume = 0
  let hasAmount = true
  for (const row of data) {
    if (typeof row.amount === 'number' && Number.isFinite(row.amount)) {
      amount += row.amount
    } else {
      hasAmount = false
    }
    volume += row.volume * volumeMultiplier
    result.push(hasAmount && volume > 0 ? amount / volume : null)
  }
  return result
}

function generateFullDayTimes(): string[] {
  const times: string[] = []
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  for (let hour = 13; hour <= 15; hour++) {
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = 0; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()

/**
 * 分时图 x 轴时间点。
 *
 * A 股/ETF/指数: 固定 242 槽 (含午休占位), 槽位与交易时段一一对应, 非交易时段留空。
 * crypto: 24x7 无固定时段 (一个合约日最多 1440 根, 北京 08:00 切日), 242 槽表达不了
 * 且会静默丢弃槽外数据, 因此直接用数据自身的时间点作为时轴, 一根都不丢。
 */
export function intradayAxisTimes(assetType: string | null | undefined, rows: MinuteKlineRow[]): string[] {
  if (!isCryptoAssetType(assetType)) return FULL_DAY_TIMES
  return rows.map(row => formatMinuteTime(row.datetime))
}

/** 价格显示小数位: A 股/ETF/指数按分 (2 位); crypto 无 0.01 最小变动单位,
 *  低价币按量级补位, 避免 toFixed(2) 把价格抹成 0.00 / Y 轴标签重复。 */
export function intradayPriceDigits(assetType: string | null | undefined, prices: (number | null | undefined)[]): number {
  if (!isCryptoAssetType(assetType)) return 2
  let max = 0
  for (const price of prices) {
    if (typeof price === 'number' && Number.isFinite(price)) max = Math.max(max, Math.abs(price))
  }
  if (max >= 1) return 2
  if (max >= 0.01) return 4
  if (max >= 0.0001) return 6
  return max > 0 ? 8 : 2
}

/** 成交量文本: A 股/ETF/指数是整数「手」; crypto 是币/张, 可为小数,
 *  直接 toFixed(0) 会把 0.4 显示成 0。 */
export function formatIntradayVolume(volume: number, assetType?: string | null): string {
  if (!Number.isFinite(volume)) return '—'
  if (!isCryptoAssetType(assetType)) return volume.toFixed(0)
  if (Number.isInteger(volume)) return volume.toFixed(0)
  const rounded = Number(volume.toFixed(6))
  return rounded === 0 ? volume.toPrecision(4) : String(rounded)
}
