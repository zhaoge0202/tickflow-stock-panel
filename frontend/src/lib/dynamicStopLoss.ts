/**
 * 动态出场与止损止盈计算引擎。
 *
 * 核心对齐双刃合/Focus 回测引擎的出场机制:
 * 1. 移动止盈 (Trailing Stop): 盘中跟踪最高价 Peak, 跌破 Peak * (1 - trailingStopPct) 出场 (默认 3.5%);
 * 2. 底线硬止损 (Hard Stop): 跌破 costPrice * (1 - stopLossPct) 出场 (默认 5.0%);
 * 3. 均线防守 (MA5): 跌破 MA5 出场;
 * 4. 时间硬约束 (Max Hold): 超过最大持仓天数出场 (默认 3 天)。
 */

export interface StopLossParams {
  trailingStopPct?: number // 默认 0.035 (3.5%)
  stopLossPct?: number     // 默认 0.05 (5.0%)
  maxHoldDays?: number     // 默认 3
  useMa5?: boolean         // 是否结合 MA5 均线防守, 默认 true
}

export interface StopLossInput {
  costPrice: number
  currentPrice: number
  peakPrice: number
  ma5?: number | null
  holdingDays?: number
  params?: StopLossParams
}

export type StopLossState = 'NORMAL' | 'WARNING' | 'TRIGGERED'

export interface StopLossCalculation {
  costPrice: number
  currentPrice: number
  peakPrice: number
  trailingStopPrice: number
  hardStopPrice: number
  ma5Price: number | null
  effectiveStopPrice: number
  effectiveExitReason: 'trailing_stop' | 'stop_loss' | 'ma5_breakdown' | 'max_hold' | 'none'
  safetyMarginPct: number  // (currentPrice - effectiveStopPrice) / currentPrice * 100
  pnlPct: number           // (currentPrice - costPrice) / costPrice * 100
  state: StopLossState
  holdingDays: number
  maxHoldDays: number
  isOverHold: boolean
}

/** 默认双刃合-Focus 冠军参数 */
export const DEFAULT_FOCUS_PARAMS: Required<StopLossParams> = {
  trailingStopPct: 0.035,
  stopLossPct: 0.05,
  maxHoldDays: 3,
  useMa5: true,
}

/**
 * 计算动态出场价位与安全垫
 */
export function calculateDynamicStopLoss(input: StopLossInput): StopLossCalculation {
  const params = { ...DEFAULT_FOCUS_PARAMS, ...input.params }
  const cost = input.costPrice > 0 ? input.costPrice : input.currentPrice
  const current = input.currentPrice
  // Peak 必须至少不低于买入成本价和当前价
  const peak = Math.max(input.peakPrice || 0, cost, current)

  const trailingStopPrice = peak * (1 - params.trailingStopPct)
  const hardStopPrice = cost * (1 - params.stopLossPct)
  const ma5Price = (params.useMa5 && input.ma5 && input.ma5 > 0) ? input.ma5 : null

  // 有效防守价取各有效防守线的最高者 (谁先被碰到谁生效)
  const candidates: Array<{ price: number; reason: 'trailing_stop' | 'stop_loss' | 'ma5_breakdown' }> = [
    { price: trailingStopPrice, reason: 'trailing_stop' },
    { price: hardStopPrice, reason: 'stop_loss' },
  ]
  if (ma5Price != null) {
    candidates.push({ price: ma5Price, reason: 'ma5_breakdown' })
  }

  // 按价格降序排列，最高者为最先防守线
  candidates.sort((a, b) => b.price - a.price)
  const highestDefense = candidates[0]

  const holdingDays = input.holdingDays ?? 1
  const isOverHold = holdingDays > params.maxHoldDays

  let effectiveStopPrice = highestDefense.price
  let effectiveExitReason: StopLossCalculation['effectiveExitReason'] = highestDefense.reason

  if (isOverHold) {
    effectiveExitReason = 'max_hold'
  }

  // 安全垫百分比: 当前价格高出防守价的百分比 (正数为安全, 负数为跌破)
  const safetyMarginPct = current > 0 ? ((current - effectiveStopPrice) / current) * 100 : 0
  const pnlPct = cost > 0 ? ((current - cost) / cost) * 100 : 0

  let state: StopLossState = 'NORMAL'
  if (current <= effectiveStopPrice || isOverHold) {
    state = 'TRIGGERED'
  } else if (safetyMarginPct <= 1.5) {
    state = 'WARNING'
  }

  return {
    costPrice: cost,
    currentPrice: current,
    peakPrice: peak,
    trailingStopPrice: Number(trailingStopPrice.toFixed(3)),
    hardStopPrice: Number(hardStopPrice.toFixed(3)),
    ma5Price: ma5Price != null ? Number(ma5Price.toFixed(3)) : null,
    effectiveStopPrice: Number(effectiveStopPrice.toFixed(3)),
    effectiveExitReason,
    safetyMarginPct: Number(safetyMarginPct.toFixed(2)),
    pnlPct: Number(pnlPct.toFixed(2)),
    state,
    holdingDays,
    maxHoldDays: params.maxHoldDays,
    isOverHold,
  }
}
