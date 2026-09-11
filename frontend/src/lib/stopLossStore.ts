import { useSyncExternalStore } from 'react'
import { calculateDynamicStopLoss, type StopLossCalculation } from './dynamicStopLoss'
import { playNotificationSound } from './notificationSound'
import { speakAlerts } from './voiceBroadcast'
import type { AlertEvent } from './api'

export interface MonitoredPosition {
  symbol: string
  name: string
  costPrice: number
  peakPrice: number
  buyDate: string
  strategyId?: string
  strategyName?: string
  currentPrice?: number
  changePct?: number
  todayHigh?: number
  ma5?: number | null
  holdingDays?: number
  lastTriggeredState?: 'NORMAL' | 'WARNING' | 'TRIGGERED'
  lastAlertedAt?: number
}

const STORAGE_KEY = 'tf_monitored_stop_loss_positions'
const PIP_OPEN_KEY = 'tf_stop_loss_pip_open'

// ===== 内存响应式 Store =====
let _positions: MonitoredPosition[] = loadPositionsFromStorage()
let _isPipOpen: boolean = loadPipOpenFromStorage()
const _listeners = new Set<() => void>()

function _emit() {
  _listeners.forEach((fn) => fn())
}

function loadPositionsFromStorage(): MonitoredPosition[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function savePositionsToStorage(items: MonitoredPosition[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
  } catch {
    /* ignore */
  }
}

function loadPipOpenFromStorage(): boolean {
  try {
    return localStorage.getItem(PIP_OPEN_KEY) === '1'
  } catch {
    return false
  }
}

function savePipOpenToStorage(open: boolean) {
  try {
    localStorage.setItem(PIP_OPEN_KEY, open ? '1' : '0')
  } catch {
    /* ignore */
  }
}

/** 订阅盯盘持仓列表 */
export function subscribeStopLossStore(listener: () => void) {
  _listeners.add(listener)
  return () => {
    _listeners.delete(listener)
  }
}

export function getMonitoredPositions(): MonitoredPosition[] {
  return _positions
}

export function getIsPipOpen(): boolean {
  return _isPipOpen
}

export function setPipOpen(open: boolean) {
  _isPipOpen = open
  savePipOpenToStorage(open)
  _emit()
}

export function togglePipOpen() {
  setPipOpen(!_isPipOpen)
}

/** 添加或更新盯盘持仓 */
export function addOrUpdateMonitoredPosition(pos: {
  symbol: string
  name: string
  costPrice: number
  buyDate?: string
  strategyId?: string
  strategyName?: string
  currentPrice?: number
  todayHigh?: number
  ma5?: number | null
}) {
  const normSymbol = pos.symbol.trim().toUpperCase()
  const todayStr = new Date().toISOString().slice(0, 10)
  const existingIdx = _positions.findIndex((p) => p.symbol.toUpperCase() === normSymbol)

  const initialCost = pos.costPrice > 0 ? pos.costPrice : (pos.currentPrice || 10)
  const initialHigh = Math.max(initialCost, pos.todayHigh || 0, pos.currentPrice || 0)

  if (existingIdx >= 0) {
    const prev = _positions[existingIdx]
    const updated: MonitoredPosition = {
      ...prev,
      name: pos.name || prev.name,
      costPrice: pos.costPrice > 0 ? pos.costPrice : prev.costPrice,
      peakPrice: Math.max(prev.peakPrice, initialHigh),
      currentPrice: pos.currentPrice ?? prev.currentPrice,
      todayHigh: pos.todayHigh ?? prev.todayHigh,
      ma5: pos.ma5 !== undefined ? pos.ma5 : prev.ma5,
      strategyId: pos.strategyId || prev.strategyId,
      strategyName: pos.strategyName || prev.strategyName,
    }
    _positions = [..._positions.slice(0, existingIdx), updated, ..._positions.slice(existingIdx + 1)]
  } else {
    const newPos: MonitoredPosition = {
      symbol: normSymbol,
      name: pos.name || normSymbol,
      costPrice: initialCost,
      peakPrice: initialHigh,
      buyDate: pos.buyDate || todayStr,
      strategyId: pos.strategyId,
      strategyName: pos.strategyName,
      currentPrice: pos.currentPrice ?? initialCost,
      todayHigh: pos.todayHigh ?? initialCost,
      ma5: pos.ma5 ?? null,
      holdingDays: 1,
    }
    _positions = [newPos, ..._positions]
  }

  savePositionsToStorage(_positions)
  _isPipOpen = true
  savePipOpenToStorage(true)
  _emit()
}

/** 移除盯盘持仓 */
export function removeMonitoredPosition(symbol: string) {
  const norm = symbol.trim().toUpperCase()
  _positions = _positions.filter((p) => p.symbol.toUpperCase() !== norm)
  savePositionsToStorage(_positions)
  _emit()
}

/** 批量更新最新行情与动态风控判定 */
export function updateMonitoredQuotes(
  quotesMap: Record<
    string,
    { currentPrice: number; high?: number; changePct?: number; ma5?: number | null }
  >,
) {
  if (_positions.length === 0) return

  let changed = false
  const now = Date.now()
  const alertsToSpeak: AlertEvent[] = []

  const nextPositions = _positions.map((item) => {
    const q = quotesMap[item.symbol] || quotesMap[item.symbol.toUpperCase()]
    if (!q) return item

    const newCurrent = q.currentPrice > 0 ? q.currentPrice : item.currentPrice
    const newHigh = Math.max(item.peakPrice, q.high || 0, newCurrent || 0)
    const newMa5 = q.ma5 !== undefined ? q.ma5 : item.ma5

    // 计算实时动态出场状态
    const calc: StopLossCalculation = calculateDynamicStopLoss({
      costPrice: item.costPrice,
      currentPrice: newCurrent || item.costPrice,
      peakPrice: newHigh,
      ma5: newMa5,
      holdingDays: item.holdingDays,
    })

    // 检查状态迁移以触发告警
    const prevState = item.lastTriggeredState || 'NORMAL'
    let lastAlertedAt = item.lastAlertedAt || 0

    if (calc.state === 'TRIGGERED' && (prevState !== 'TRIGGERED' || now - lastAlertedAt > 300_000)) {
      // 触发警报音与语音播报 (防抖: 5分钟内同一只不重复轰炸)
      lastAlertedAt = now
      const reasonDesc =
        calc.effectiveExitReason === 'trailing_stop'
          ? `触及移动止盈线 ${calc.trailingStopPrice} 元 (高点回撤 3.5%)`
          : calc.effectiveExitReason === 'stop_loss'
            ? `跌破底线止损 ${calc.hardStopPrice} 元 (-5%)`
            : calc.effectiveExitReason === 'ma5_breakdown'
              ? `跌破 MA5 均线 ${calc.ma5Price} 元`
              : '持仓到达上限 3 天'

      alertsToSpeak.push({
        ts: now,
        source: 'strategy',
        type: 'sell_signal',
        symbol: item.symbol,
        name: item.name,
        message: `${item.name} 触发离场出场: ${reasonDesc}`,
        price: newCurrent,
        change_pct: q.changePct ?? 0,
        severity: 'critical',
      })
    }

    if (
      newCurrent !== item.currentPrice ||
      newHigh !== item.peakPrice ||
      q.changePct !== item.changePct ||
      calc.state !== item.lastTriggeredState
    ) {
      changed = true
      return {
        ...item,
        currentPrice: newCurrent,
        peakPrice: newHigh,
        changePct: q.changePct ?? item.changePct,
        ma5: newMa5,
        lastTriggeredState: calc.state,
        lastAlertedAt,
      }
    }
    return item
  })

  if (changed) {
    _positions = nextPositions
    savePositionsToStorage(_positions)
    _emit()
  }

  if (alertsToSpeak.length > 0) {
    playNotificationSound()
    speakAlerts(alertsToSpeak)
  }
}

/** React Hook: 读取持仓列表 */
export function useMonitoredPositions(): MonitoredPosition[] {
  return useSyncExternalStore(subscribeStopLossStore, getMonitoredPositions, () => [])
}

/** React Hook: 读取 PiP 浮窗是否显示 */
export function useIsPipOpen(): boolean {
  return useSyncExternalStore(subscribeStopLossStore, getIsPipOpen, () => false)
}
