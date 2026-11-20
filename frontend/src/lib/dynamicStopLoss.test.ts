import { describe, expect, it } from 'vitest'
import { calculateDynamicStopLoss } from './dynamicStopLoss'

const input = { costPrice: 100, currentPrice: 98, peakPrice: 120, holdingDays: 2 }
// 合成测试参数，与用户运行配置无关。
const policy = { trailingStopPct: 0.12, stopLossPct: 0.2, maxHoldDays: 7, useMa5: false }

describe('runtime stop-loss policy', () => {
  it('does not invent a policy when local configuration is absent', () => {
    const result = calculateDynamicStopLoss(input)
    expect(result.configured).toBe(false)
    expect(result.state).toBe('NORMAL')
    expect(result.effectiveExitReason).toBe('none')
    expect(Number.isFinite(result.effectiveStopPrice)).toBe(false)
  })

  it('uses injected price and holding limits', () => {
    const result = calculateDynamicStopLoss({ ...input, params: policy })
    expect(result.configured).toBe(true)
    expect(result.trailingStopPrice).toBeCloseTo(105.6)
    expect(result.hardStopPrice).toBe(80)
    expect(result.state).toBe('TRIGGERED')
    expect(result.effectiveExitReason).toBe('trailing_stop')
    expect(calculateDynamicStopLoss({ ...input, holdingDays: 8, params: policy }).effectiveExitReason).toBe('max_hold')
  })

  it('honors the explicit moving-average setting', () => {
    expect(calculateDynamicStopLoss({ ...input, ma5: 110, params: policy }).ma5Price).toBeNull()
    expect(calculateDynamicStopLoss({ ...input, ma5: 110, params: { ...policy, useMa5: true } }).effectiveExitReason).toBe('ma5_breakdown')
  })

  it('rejects malformed policies without producing a sell signal', () => {
    for (const params of [{ ...policy, trailingStopPct: Number.NaN }, { ...policy, maxHoldDays: 0 }]) {
      const result = calculateDynamicStopLoss({ ...input, params })
      expect(result.configured).toBe(false)
      expect(result.state).toBe('NORMAL')
    }
  })
})
