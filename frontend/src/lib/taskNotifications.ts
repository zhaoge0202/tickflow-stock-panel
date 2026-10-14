import { toast } from '@/components/Toast'
import { playNotificationSound } from './notificationSound'
import type { StrategyBacktestResult } from './api'
import type { OptimizeResult } from './optimizerTask'

function pct(value: unknown): string {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : '—'
}

function desktopNotification(title: string, message: string): void {
  try {
    if (
      typeof window === 'undefined'
      || typeof Notification === 'undefined'
      || Notification.permission !== 'granted'
      || document.visibilityState === 'visible'
    ) return
    new Notification(title, { body: message })
  } catch {
    // 浏览器通知不可用时保留 Toast 和提示音。
  }
}

/** 在开始按钮的用户手势中调用，允许后台任务完成后弹系统通知。 */
export function requestTaskNotificationPermission(): void {
  try {
    if (
      typeof window !== 'undefined'
      && typeof Notification !== 'undefined'
      && Notification.permission === 'default'
    ) {
      void Notification.requestPermission()
    }
  } catch {
    // 权限 API 不可用时不影响任务运行。
  }
}

export function notifyTaskError(title: string, message: string): void {
  const text = `${title}：${message}`
  toast(text, 'error')
  playNotificationSound()
  desktopNotification(title, message)
}

export function notifyBacktestCompleted(result: StrategyBacktestResult): void {
  const name = result.strategy_info?.name || result.config?.strategy_id || '策略'
  if (result.error) {
    notifyTaskError(`${name}回测失败`, result.error)
    return
  }
  const stats = result.stats || {}
  const message = `${name}回测完成 · 收益 ${pct(stats.total_return)} · 胜率 ${pct(stats.win_rate)} · ${stats.n_trades ?? '—'} 笔`
  toast(message, 'success')
  playNotificationSound()
  desktopNotification('TickFlow · 回测完成', message)
}

export function notifyOptimizerCompleted(result: OptimizeResult): void {
  const message = `参数优化完成 · ${result.n_completed}/${result.n_combinations} 组 · 最优值 ${result.best_score ?? '—'}`
  toast(message, 'success')
  playNotificationSound()
  desktopNotification('TickFlow · 优化完成', message)
}
