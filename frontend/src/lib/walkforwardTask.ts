import { useSyncExternalStore } from 'react'

/** Walk-forward 任务管理 (SSE + job_key 回吐 + 重连)。镜像 optimizerTask。 */

export interface WFProgress {
  type: string
  done: number
  total: number
  fold: number
}

export interface WFFold {
  index: number
  train_start: string
  train_end: string
  test_start: string
  test_end: string
  best_params: Record<string, any> | null
  is_score: number | null
  oos_objective: number | null
  oos_stats: Record<string, any>
}

export interface WFSummary {
  n_folds: number
  compounded_oos_return: number
  avg_is_objective: number | null
  avg_oos_objective: number | null
  degradation: number | null
  consistency: number
  oos_equity_curve: { fold: number; date: string; value: number }[]
}

export interface WalkForwardResult {
  objective: string
  n_folds: number
  n_planned_folds: number
  folds: WFFold[]
  summary: WFSummary
  elapsed_ms: number
}

export interface WalkForwardTask {
  id: number
  isPending: boolean
  result: WalkForwardResult | null
  progress: WFProgress | null
  error: string | null
}

export interface StartWalkForwardParams {
  strategy_id: string
  param_grid: Record<string, any>
  objective: string
  train_days: number
  test_days: number
  step_days: number
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  mode?: 'position' | 'full'
}

let current: WalkForwardTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null
let currentJobKey: string | null = null

const RECONNECT_KEY = 'walkforward_reconnect'
const JOB_KEY_KEY = 'walkforward_job_key'

function emit() {
  listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') sp.set(k, String(v))
  }
  return sp.toString()
}

function connectSSE(url: string): void {
  const id = current?.id ?? ++taskSeq

  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const es = new EventSource(url)
  eventSource = es

  es.addEventListener('job', (e: MessageEvent) => {
    try {
      const key = JSON.parse(e.data)?.key
      if (key) {
        currentJobKey = key
        localStorage.setItem(JOB_KEY_KEY, key)
      }
    } catch { /* ignore */ }
  })

  es.addEventListener('progress', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const prog = JSON.parse(e.data) as WFProgress
      current = { ...current, progress: prog }
      emit()
    } catch { /* ignore */ }
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse(e.data) as WalkForwardResult
      current = { ...current, isPending: false, result, error: null }
      emit()
    } catch {
      current = { ...current, isPending: false, error: '结果解析失败' }
      emit()
    }
    es.close()
    eventSource = null
    currentJobKey = null
    localStorage.removeItem(RECONNECT_KEY)
    localStorage.removeItem(JOB_KEY_KEY)
  })

  es.addEventListener('error', (e: MessageEvent) => {
    if (current?.id !== id) return
    if (e.data) {
      try {
        const msg = JSON.parse(e.data)?.message ?? 'walk-forward 出错'
        current = { ...current, isPending: false, error: msg }
        emit()
      } catch {
        current = { ...current, isPending: false, error: 'walk-forward 出错' }
        emit()
      }
      es.close()
      eventSource = null
      currentJobKey = null
      localStorage.removeItem(RECONNECT_KEY)
      localStorage.removeItem(JOB_KEY_KEY)
    }
  })
}

export function startWalkForward(params: StartWalkForwardParams): void {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null }
  emit()

  const qs = buildQuery({
    strategy_id: params.strategy_id,
    param_grid: JSON.stringify(params.param_grid),
    objective: params.objective,
    train_days: params.train_days,
    test_days: params.test_days,
    step_days: params.step_days,
    symbols: params.symbols?.join(','),
    start: params.start ?? undefined,
    end: params.end ?? undefined,
    mode: params.mode,
  })

  localStorage.setItem(RECONNECT_KEY, qs)
  connectSSE(`/api/backtest/walkforward/stream?${qs}`)
}

export async function stopWalkForward(): Promise<void> {
  const jobKey = currentJobKey ?? localStorage.getItem(JOB_KEY_KEY)
  if (jobKey) {
    await fetch('/api/backtest/walkforward/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_key: jobKey }),
    }).catch(() => {})
  }
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  if (current?.isPending) {
    current = { ...current, isPending: false, error: '已取消' }
    emit()
  }
  currentJobKey = null
  localStorage.removeItem(RECONNECT_KEY)
  localStorage.removeItem(JOB_KEY_KEY)
}

export function clearWalkForward(): void {
  current = null
  emit()
}

export function tryReconnectWalkForward(): boolean {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (!qs) return false
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null }
  emit()
  connectSSE(`/api/backtest/walkforward/stream?${qs}`)
  return true
}

export function useWalkForwardTask(): WalkForwardTask | null {
  return useSyncExternalStore(subscribe, () => current, () => null)
}
