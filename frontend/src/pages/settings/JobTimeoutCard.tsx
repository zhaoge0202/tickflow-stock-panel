/**
 * 数据任务超时配置卡片 — 从 DataSources 抽出, 放在系统设置页。
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Clock3 } from 'lucide-react'
import { api, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'
import { toast } from '@/components/Toast'

type TimeoutUnit = 'second' | 'minute' | 'hour'

const TIMEOUT_UNIT_SECONDS: Record<TimeoutUnit, number> = {
  second: 1,
  minute: 60,
  hour: 3600,
}

function preferredTimeoutUnit(seconds: number): TimeoutUnit {
  if (seconds >= 3600 && seconds % 1800 === 0) return 'hour'
  if (seconds % 60 === 0) return 'minute'
  return 'second'
}

function formatTimeoutValue(seconds: number, unit: TimeoutUnit): string {
  if (!Number.isFinite(seconds)) return ''
  const value = seconds / TIMEOUT_UNIT_SECONDS[unit]
  return String(Number(value.toFixed(4)))
}

export function JobTimeoutCard() {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const [timeoutDraft, setTimeoutDraft] = useState<{ regular: string; long: string } | null>(null)
  const [regularUnitOverride, setRegularUnitOverride] = useState<TimeoutUnit | null>(null)
  const [longUnitOverride, setLongUnitOverride] = useState<TimeoutUnit | null>(null)

  const currentRegularTimeout = prefs.data?.data_source_job_timeout_s ?? 1200
  const currentLongTimeout = prefs.data?.data_source_long_job_timeout_s ?? 1800
  const regularTimeoutUnit = regularUnitOverride ?? preferredTimeoutUnit(currentRegularTimeout)
  const longTimeoutUnit = longUnitOverride ?? preferredTimeoutUnit(currentLongTimeout)
  const regularTimeoutInput = timeoutDraft?.regular
    ?? formatTimeoutValue(currentRegularTimeout, regularTimeoutUnit)
  const longTimeoutInput = timeoutDraft?.long
    ?? formatTimeoutValue(currentLongTimeout, longTimeoutUnit)
  const regularInputNumber = Number(regularTimeoutInput)
  const longInputNumber = Number(longTimeoutInput)
  const regularTimeout = Math.round(regularInputNumber * TIMEOUT_UNIT_SECONDS[regularTimeoutUnit])
  const longTimeout = Math.round(longInputNumber * TIMEOUT_UNIT_SECONDS[longTimeoutUnit])
  const timeoutValuesValid = Number.isFinite(regularInputNumber) && regularInputNumber > 0
    && Number.isFinite(longInputNumber) && longInputNumber > 0
    && regularTimeout >= 60 && longTimeout >= 60
  const timeoutValuesChanged = regularTimeout !== currentRegularTimeout
    || longTimeout !== currentLongTimeout

  const saveJobTimeouts = useMutation({
    mutationFn: () => api.updateDataSourceJobTimeouts(regularTimeout, longTimeout),
    onSuccess: (saved) => {
      qc.setQueryData<Preferences>(QK.preferences, current => (
        current ? { ...current, ...saved } : current
      ))
      setTimeoutDraft(null)
      toast('任务超时配置已保存', 'success')
    },
    onError: (e: Error) => toast(`保存失败: ${e.message}`, 'error'),
  })

  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div className="flex items-start gap-2.5">
          <Clock3 className="h-4 w-4 text-secondary mt-0.5" />
          <div>
            <h2 className="text-sm font-medium text-foreground">超时设置</h2>
            <p className="text-[11px] text-muted mt-1 leading-relaxed">
              后台任务超过对应时间<b>没有任何进度</b>才判定卡死并自动终止；只要任务仍在推进（如慢带宽下的冷启动全市场拉取），无论总时长多久都不会被中断。保存时自动换算为秒，修改后对新建任务生效。
            </p>
          </div>
        </div>
        <button
          onClick={() => saveJobTimeouts.mutate()}
          disabled={!timeoutValuesValid || !timeoutValuesChanged || saveJobTimeouts.isPending}
          className="shrink-0 px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium hover:bg-accent/90 disabled:opacity-40 transition-colors"
        >
          {saveJobTimeouts.isPending ? '保存中...' : '保存'}
        </button>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="rounded-lg border border-border/60 bg-elevated/20 px-3.5 py-3">
          <span className="block text-xs font-medium text-foreground mb-1">普通任务停滞超时</span>
          <span className="block text-[10px] text-muted mb-2">日 K 管道、扩展、修正与重算任务</span>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={regularTimeoutUnit === 'second' ? 60 : regularTimeoutUnit === 'minute' ? 1 : 1 / 60}
              step={regularTimeoutUnit === 'second' ? 60 : regularTimeoutUnit === 'minute' ? 1 : 0.5}
              value={regularTimeoutInput}
              onChange={e => setTimeoutDraft({ regular: e.target.value, long: longTimeoutInput })}
              className="w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm text-foreground font-mono outline-none focus:border-accent"
            />
            <select
              value={regularTimeoutUnit}
              onChange={e => {
                const nextUnit = e.target.value as TimeoutUnit
                setTimeoutDraft({
                  regular: formatTimeoutValue(regularTimeout, nextUnit),
                  long: longTimeoutInput,
                })
                setRegularUnitOverride(nextUnit)
              }}
              className="w-20 shrink-0 rounded-btn border border-border bg-base px-2 py-1.5 text-xs text-foreground outline-none focus:border-accent"
            >
              <option value="second">秒</option>
              <option value="minute">分钟</option>
              <option value="hour">小时</option>
            </select>
          </div>
          <span className="block text-[10px] text-muted/60 mt-1.5">默认 20 分钟无进度，最小 1 分钟</span>
        </label>

        <label className="rounded-lg border border-border/60 bg-elevated/20 px-3.5 py-3">
          <span className="block text-xs font-medium text-foreground mb-1">长任务停滞超时</span>
          <span className="block text-[10px] text-muted mb-2">分钟 K 全市场同步任务</span>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={longTimeoutUnit === 'second' ? 60 : longTimeoutUnit === 'minute' ? 1 : 1 / 60}
              step={longTimeoutUnit === 'second' ? 60 : longTimeoutUnit === 'minute' ? 1 : 0.5}
              value={longTimeoutInput}
              onChange={e => setTimeoutDraft({ regular: regularTimeoutInput, long: e.target.value })}
              className="w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm text-foreground font-mono outline-none focus:border-accent"
            />
            <select
              value={longTimeoutUnit}
              onChange={e => {
                const nextUnit = e.target.value as TimeoutUnit
                setTimeoutDraft({
                  regular: regularTimeoutInput,
                  long: formatTimeoutValue(longTimeout, nextUnit),
                })
                setLongUnitOverride(nextUnit)
              }}
              className="w-20 shrink-0 rounded-btn border border-border bg-base px-2 py-1.5 text-xs text-foreground outline-none focus:border-accent"
            >
              <option value="second">秒</option>
              <option value="minute">分钟</option>
              <option value="hour">小时</option>
            </select>
          </div>
          <span className="block text-[10px] text-muted/60 mt-1.5">默认 30 分钟无进度，最小 1 分钟</span>
        </label>
      </div>
    </section>
  )
}
