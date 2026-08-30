import { useState, useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Trash2, Download, Calendar, Zap } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MissingCapChip } from '@/lib/capability-labels'

export function MinuteSyncConfig({ caps, onJobStart }: { caps: { label: string; capabilities: Record<string, { rpm: number | null; batch: number | null; subscribe: number | null }> } | undefined; onJobStart?: (jobId: string) => void }) {
  const qc = useQueryClient()
  const prefs = useQuery({
    queryKey: QK.preferences,
    queryFn: api.preferences,
  })
  const update = useMutation({
    mutationFn: ({ enabled, days, segmentDays, refresh }: {
      enabled: boolean; days: number; segmentDays?: number
      refresh?: { enabled?: boolean; interval?: number }
    }) =>
      api.updateMinuteSync(enabled, days, segmentDays, refresh),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: ['minute-refresh-status'] })
    },
  })

  // 盘中增量刷新状态 (轮询 15s, 仅弹窗挂载期间)
  const refreshStatus = useQuery({
    queryKey: ['minute-refresh-status'],
    queryFn: api.minuteRefreshStatus,
    refetchInterval: 15000,
  })

  const hasMinuteCap = !!caps?.capabilities?.['kline.minute.batch']
  const enabled = prefs.data?.minute_sync_enabled ?? false
  const days = prefs.data?.minute_sync_days ?? 5
  const segmentDays = prefs.data?.minute_sync_segment_days ?? 20
  const refreshEnabled = prefs.data?.minute_refresh_enabled ?? false
  const refreshInterval = prefs.data?.minute_refresh_interval ?? 60
  const [localDays, setLocalDays] = useState(days)
  const [localSegment, setLocalSegment] = useState(segmentDays)
  const [localRefreshInterval, setLocalRefreshInterval] = useState(refreshInterval)

  useEffect(() => { setLocalDays(days) }, [days])
  useEffect(() => { setLocalSegment(segmentDays) }, [segmentDays])
  useEffect(() => { setLocalRefreshInterval(refreshInterval) }, [refreshInterval])

  // 盘中增量 = intraday.batch 独立能力 (Expert 专有), 与盘后同步的 minute.batch 分属不同限流池
  const hasIntradayBatchCap = !!caps?.capabilities?.['intraday.batch']
  const rs = refreshStatus.data
  const refreshGateText = rs?.custom_provider_active
    ? '已配置自定义分钟源, 盘中增量由插件自管'
    : rs && rs.available && !rs.capability_ok
      ? '需要日内分时批量能力 (Expert)'
      : !rs?.in_trading_hours ? '非连续竞价时段, 暂停中'
      : rs?.last_error ? `最近错误: ${rs.last_error}`
      : null

  const handleRefreshToggle = () => {
    if (!hasIntradayBatchCap) return
    update.mutate({ enabled, days: localDays, refresh: { enabled: !refreshEnabled } })
  }

  const setRefreshInterval = (v: number) => {
    const clamped = Math.max(60, Math.min(300, Math.round(v / 30) * 30))
    setLocalRefreshInterval(clamped)
    update.mutate({ enabled, days: localDays, refresh: { interval: clamped } })
  }

  useEffect(() => { setLocalDays(days) }, [days])
  useEffect(() => { setLocalSegment(segmentDays) }, [segmentDays])

  const handleToggle = () => {
    if (!hasMinuteCap) return
    update.mutate({ enabled: !enabled, days: localDays })
  }

  const setDays = (v: number) => {
    const clamped = Math.max(1, Math.min(30, v))
    setLocalDays(clamped)
    update.mutate({ enabled, days: clamped })
  }

  // 段大小: 交易日/段, 步进 ±5, 范围 [5, 30]。越小越省内存但越慢。
  const setSegment = (v: number) => {
    const clamped = Math.max(5, Math.min(30, v))
    setLocalSegment(clamped)
    update.mutate({ enabled, days: localDays, segmentDays: clamped })
  }

  // 清空分钟K数据 (二次确认)
  const [confirmClear, setConfirmClear] = useState(false)
  const clearMutation = useMutation({
    mutationFn: () => api.clearMinute(),
    onSuccess: () => {
      setConfirmClear(false)
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
  })

  // 手动获取 (两个独立按钮, 各自指定天数, 不影响自动同步偏好)
  const [fetchingMode, setFetchingMode] = useState<'' | '40d' | '1y'>('')
  const handleFetch = (mode: '40d' | '1y') => {
    if (!hasMinuteCap) return
    // 单次获取 = 按「分段大小」拉一段 (向前扩展); 1年 = 拉365天按分段切多段
    const fetchDays = mode === '40d' ? localSegment : 365
    setFetchingMode(mode)
    api.syncMinute(fetchDays, true).then((res) => {
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      // 通知主页面跟踪 job 进度 (ActiveJobCard 会显示实时进度+日志)
      if (res.job_id && onJobStart) onJobStart(res.job_id)
    }).finally(() => setFetchingMode(''))
  }

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-3">
      {/* 区块 A: 自动同步 (盘后定时拉取的偏好设置) */}
      <div className="space-y-2.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <button
            onClick={handleToggle}
            disabled={!hasMinuteCap}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-200 shrink-0 ${
              enabled ? 'bg-accent shadow-[0_0_6px_rgba(61,214,140,0.3)]' : 'bg-elevated'
            } ${!hasMinuteCap ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                enabled ? 'translate-x-[18px]' : 'translate-x-0.5'
              }`}
            />
          </button>
          <span className="text-xs text-foreground font-medium">
            自动同步{enabled ? '已开启' : '已关闭'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center">
            <button
              onClick={() => setDays(localDays - 1)}
              disabled={!hasMinuteCap || !enabled || localDays <= 1}
              className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >−</button>
            <div className={`h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums ${enabled ? 'text-foreground bg-base' : 'text-muted bg-elevated/50'}`}>
              {localDays}
            </div>
            <button
              onClick={() => setDays(localDays + 1)}
              disabled={!hasMinuteCap || !enabled || localDays >= 30}
              className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >+</button>
          </div>
          <span className="text-[10px] text-muted">天</span>
          {!hasMinuteCap && (
            <MissingCapChip capKey="kline.minute.batch" />
          )}
        </div>
      </div>

      {/* 分段大小: 控制单次 SDK 请求覆盖的交易日数。每段拉完即落盘,避免全量攒内存 OOM。
          「往前获取」与「获取 1 年」共用此设置 (两者都经过 sync_and_persist_minute)。 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-foreground font-medium">分段大小</span>
          <span className="text-[10px] text-muted px-1 py-px rounded bg-warning/8 text-warning/80">内存优化</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center">
            <button
              onClick={() => setSegment(localSegment - 5)}
              disabled={!hasMinuteCap || localSegment <= 5}
              className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >−</button>
            <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums bg-base text-foreground">
              {localSegment}
            </div>
            <button
              onClick={() => setSegment(localSegment + 5)}
              disabled={!hasMinuteCap || localSegment >= 30}
              className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >+</button>
          </div>
          <span className="text-[10px] text-muted">交易日/段</span>
        </div>
      </div>
      <div className="text-[10px] text-muted leading-relaxed -mt-1">
        每段拉完即写盘,避免内存堆积。越小越省内存但越慢,默认 20 平衡。
      </div>
      </div>

      {/* 区块 A2: 盘中增量刷新 (Expert 专有, intraday.batch 独立限流池) */}
      <div className="pt-3 border-t border-border space-y-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <button
              onClick={handleRefreshToggle}
              disabled={!hasIntradayBatchCap}
              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-200 shrink-0 ${
                refreshEnabled ? 'bg-amber-400 shadow-[0_0_6px_rgba(245,158,11,0.3)]' : 'bg-elevated'
              } ${!hasIntradayBatchCap ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
            >
              <span
                className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                  refreshEnabled ? 'translate-x-[18px]' : 'translate-x-0.5'
                }`}
              />
            </button>
            <div className="flex items-center gap-1.5">
              <Zap className="h-3 w-3 text-amber-400" />
              <span className="text-xs text-foreground font-medium">
                盘中增量刷新{refreshEnabled ? '已开启' : '已关闭'}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center">
              <button
                onClick={() => setRefreshInterval(localRefreshInterval - 30)}
                disabled={!hasIntradayBatchCap || !refreshEnabled || localRefreshInterval <= 60}
                className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
              >−</button>
              <div className={`h-6 w-10 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums ${refreshEnabled ? 'text-foreground bg-base' : 'text-muted bg-elevated/50'}`}>
                {Math.round(localRefreshInterval / 60) >= 1 && localRefreshInterval % 60 === 0 ? `${localRefreshInterval / 60}m` : `${localRefreshInterval}s`}
              </div>
              <button
                onClick={() => setRefreshInterval(localRefreshInterval + 30)}
                disabled={!hasIntradayBatchCap || !refreshEnabled || localRefreshInterval >= 300}
                className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
              >+</button>
            </div>
            {!hasIntradayBatchCap && <MissingCapChip capKey="intraday.batch" />}
          </div>
        </div>
        <div className="text-[10px] text-muted leading-relaxed">
          交易时段内用日内分时批量 (独立配额) 每 {Math.round(localRefreshInterval / 60) >= 1 && localRefreshInterval % 60 === 0 ? `${localRefreshInterval / 60} 分钟` : `${localRefreshInterval} 秒`} 全市场脉冲落盘一次,
          分钟策略读到最新K线; 不占用盘后分钟同步的限流配额。
        </div>
        {/* 运行状态一行: 门控原因 / 下一轮 / 最近一轮 */}
        {rs?.available && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted">
            <span className={rs.running ? 'text-emerald-400' : ''}>
              ● {rs.running ? '服务运行中' : '服务未运行'}
            </span>
            {refreshGateText && <span className="text-amber-400/80">{refreshGateText}</span>}
            {rs.rounds != null && rs.rounds > 0 && (
              <span>已 {rs.rounds} 轮 · 最近 {rs.last_symbols} 标的 / {rs.last_rows} 行 / {rs.last_requests} 请求{rs.last_round_ms != null ? ` · ${(rs.last_round_ms / 1000).toFixed(1)}s` : ''}</span>
            )}
          </div>
        )}
      </div>

      {/* 区块 B: 手动获取 (一次性操作, 独立于上方自动同步开关) */}
      <div className="pt-3 border-t border-border space-y-2">
        <div className="flex items-center gap-1.5">
          <Download className="h-3 w-3 text-secondary" />
          <span className="text-[11px] text-secondary font-medium">手动获取</span>
          <span className="text-[10px] text-muted">不受自动同步开关影响</span>
        </div>
        <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => handleFetch('40d')}
          disabled={!hasMinuteCap || fetchingMode !== ''}
          className="inline-flex flex-col items-center justify-center gap-0.5 px-2 py-2 rounded-btn bg-accent/90 text-foreground text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors duration-150"
        >
          {fetchingMode === '40d' ? (
            <><Loader2 className="h-3.5 w-3.5 animate-spin" /><span>获取中…</span></>
          ) : (
            <><Download className="h-3.5 w-3.5" /><span>单次获取 {localSegment} 天</span></>
          )}
        </button>
        <button
          onClick={() => handleFetch('1y')}
          disabled={!hasMinuteCap || fetchingMode !== ''}
          className="inline-flex flex-col items-center justify-center gap-0.5 px-2 py-2 rounded-btn border border-amber-400/40 bg-amber-400/10 text-amber-400 text-xs font-medium hover:bg-amber-400/20 disabled:opacity-40 transition-colors duration-150"
        >
          {fetchingMode === '1y' ? (
            <><Loader2 className="h-3.5 w-3.5 animate-spin" /><span>分段获取中…</span></>
          ) : (
            <><Calendar className="h-3.5 w-3.5" /><span>获取最近 1 年</span><span className="text-[9px] opacity-70">分段拉取</span></>
          )}
        </button>
        </div>
        <div className="text-[10px] text-muted leading-relaxed">
          A股标的 · 前复权价格 · 从本地最早数据向前叠加 ·{' '}
          均按上方「分段大小」分段拉取、每段即落盘
        </div>
      </div>

      {/* 区块 C: 清空 (危险操作, 独立分隔) */}
      <button
        onClick={() => setConfirmClear(true)}
        disabled={clearMutation.isPending}
        title="清空分钟K数据"
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn border border-danger/30 text-danger/80 text-xs font-medium hover:bg-danger/10 disabled:opacity-40 transition-colors duration-150"
      >
        <Trash2 className="h-3 w-3" />
        清空分钟K数据
      </button>

      {/* 清空确认弹窗 */}
      {confirmClear && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => !clearMutation.isPending && setConfirmClear(false)} />
          <div className="relative rounded-card border border-border bg-surface shadow-2xl mx-4 px-6 py-5 max-w-sm w-full space-y-4">
            <div className="text-sm text-foreground text-center font-medium">确认清空分钟K数据？</div>
            <div className="text-[11px] text-muted text-center leading-relaxed">
              此操作仅删除分钟K (kline_minute) 数据, <span className="text-foreground/80">不影响</span>日K、复权因子、指标等其他数据。清空后可重新获取。
            </div>
            <div className="flex items-center justify-center gap-3">
              <button onClick={() => setConfirmClear(false)} disabled={clearMutation.isPending}
                className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors duration-150">取消</button>
              <button onClick={() => clearMutation.mutate()} disabled={clearMutation.isPending}
                className="px-4 py-1.5 rounded-btn bg-danger/90 text-foreground text-xs font-medium hover:bg-danger disabled:opacity-40 transition-colors duration-150">
                {clearMutation.isPending ? <span className="inline-flex items-center gap-1.5"><Loader2 className="h-3 w-3 animate-spin" />清空中…</span> : '确认清空'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
