import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  CheckCircle2,
  AlertTriangle,
  Clock,
  Zap,
  RefreshCw,
  Database,
  Check,
} from 'lucide-react'
import { api, type FocusReadinessResponse } from '@/lib/api'
import { toast } from '@/components/Toast'

interface Props {
  asOf?: string
  className?: string
  compact?: boolean
}

export function FocusPipelineHealthCard({ asOf, className = '', compact = false }: Props) {
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)

  const { data: readiness, isFetching, refetch } = useQuery<FocusReadinessResponse>({
    queryKey: ['focusReadiness', asOf],
    queryFn: () => api.focusReadiness(asOf),
    refetchInterval: 15_000,
  })

  const fastReconstruct = useMutation({
    mutationFn: () => api.fastReconstructToday(readiness?.as_of ?? asOf),
    onSuccess: (data) => {
      toast(
        `极速聚合完成！共生成 ${data.symbols_reconstructed} 只标的日K与指标，耗时 ${data.elapsed_seconds}s`,
        'success',
      )
      qc.invalidateQueries({ queryKey: ['focusReadiness'] })
      qc.invalidateQueries({ queryKey: ['dataStatus'] })
      qc.invalidateQueries({ queryKey: ['screenerFocusVersions'] })
      qc.invalidateQueries({ queryKey: ['screenerCachedSummary'] })
      qc.invalidateQueries({ queryKey: ['screenerCached'] })
    },
    onError: (err: any) => {
      toast(`自愈聚合失败：${err?.message ?? '未知错误'}`, 'error')
    },
  })

  const handleManualRefresh = async () => {
    setRefreshing(true)
    try {
      await refetch()
      toast('数据健康状态已刷新', 'success')
    } finally {
      setRefreshing(false)
    }
  }

  if (!readiness) {
    return (
      <div className={`p-4 rounded-xl border border-border/60 bg-surface/50 animate-pulse ${className}`}>
        <div className="h-5 w-48 bg-muted/20 rounded mb-2" />
        <div className="h-4 w-96 bg-muted/10 rounded" />
      </div>
    )
  }

  const { health, health_message, layers, focus_stages, can_fast_reconstruct, as_of, total_universe } = readiness

  const healthBadge = () => {
    switch (health) {
      case 'healthy':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-bull/15 text-bull border border-bull/30">
            <CheckCircle2 className="h-3.5 w-3.5" />
            链路完全就绪 (100%)
          </span>
        )
      case 'can_reconstruct':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30 animate-pulse">
            <AlertTriangle className="h-3.5 w-3.5" />
            分钟K齐全 · 可极速自愈补齐日K
          </span>
        )
      case 'degraded':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-warning/15 text-warning border border-warning/30">
            <AlertTriangle className="h-3.5 w-3.5" />
            部分数据残缺
          </span>
        )
      default:
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-muted/15 text-muted border border-border">
            <Clock className="h-3.5 w-3.5" />
            待生成数据
          </span>
        )
    }
  }

  return (
    <div
      className={`rounded-2xl border ${
        health === 'can_reconstruct'
          ? 'border-amber-500/40 bg-amber-500/[0.03]'
          : health === 'healthy'
          ? 'border-bull/30 bg-bull/[0.02]'
          : 'border-border/80 bg-elevated/20'
      } p-5 space-y-4 transition-all ${className}`}
    >
      {/* 顶栏：标题、日期、状态与动作按钮 */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/50 pb-3.5">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-accent/10 border border-accent/20 text-accent">
            <Activity className="h-5 w-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground tracking-tight">
                Focus 策略实时数据链路与全景大盘
              </h3>
              <span className="font-mono text-xs px-2 py-0.5 rounded bg-surface border border-border text-secondary">
                {as_of}
              </span>
            </div>
            <p className="text-xs text-muted mt-0.5">{health_message}</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {healthBadge()}

          {can_fast_reconstruct && (
            <button
              type="button"
              disabled={fastReconstruct.isPending}
              onClick={() => fastReconstruct.mutate()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-500 hover:bg-amber-400 text-black shadow-sm transition-all cursor-pointer disabled:opacity-50"
              title="本地已拥有5553只股票的全天分钟K，0.5秒内直接聚合生成全量收盘日K并重新定版Focus出票"
            >
              <Zap className="h-3.5 w-3.5 fill-black" />
              {fastReconstruct.isPending ? '极速自愈中…' : '极速补全今日日K (0.5s)'}
            </button>
          )}

          <button
            type="button"
            onClick={handleManualRefresh}
            disabled={isFetching || refreshing}
            className="p-1.5 rounded-lg border border-border bg-surface text-secondary hover:text-foreground hover:bg-elevated transition-colors cursor-pointer"
            title="刷新数据健康状态"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching || refreshing ? 'animate-spin text-accent' : ''}`} />
          </button>
        </div>
      </div>

      {/* Focus 策略全天四道生命线时序关卡 */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-2.5">
        {/* 1. 09:25 竞价撮合 */}
        <div className="p-3 rounded-xl bg-surface/60 border border-border/70 space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-secondary flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-sky-400" />
              09:25 集合竞价
            </span>
            {focus_stages.auction_0925.status === 'ready' ? (
              <span className="text-[11px] text-bull flex items-center gap-0.5">
                <Check className="h-3 w-3" /> 就绪
              </span>
            ) : (
              <span className="text-[11px] text-muted">待确认</span>
            )}
          </div>
          <div className="text-xs font-medium text-foreground">竞价高开与承接确认</div>
          <div className="text-[11px] text-muted">
            {layers.quote_ticks.status === 'ready'
              ? `逐笔撮合已入库 (${layers.quote_ticks.count} 条)`
              : '等待次日集合竞价'}
          </div>
        </div>

        {/* 2. 14:50 尾盘初选 */}
        <div className="p-3 rounded-xl bg-surface/60 border border-border/70 space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-secondary flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
              14:50 尾盘初选
            </span>
            {focus_stages.preview_1450.status === 'ready' ? (
              <span className="text-[11px] text-amber-400 font-medium">已生成</span>
            ) : (
              <span className="text-[11px] text-muted">待聚合</span>
            )}
          </div>
          <div className="text-xs font-medium text-foreground">
            候选池 {focus_stages.preview_1450.total ?? 0} 只
          </div>
          <div className="text-[11px] text-muted">
            {layers.minute.count >= 5000
              ? `分钟线已覆盖 ${layers.minute.count} 只标的`
              : '需 14:50 实时分钟K聚合'}
          </div>
        </div>

        {/* 3. 15:00 收盘日K闭环 */}
        <div className="p-3 rounded-xl bg-surface/60 border border-border/70 space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-secondary flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-bull" />
              15:00 收盘日K
            </span>
            <span
              className={`text-[11px] font-medium ${
                layers.daily.status === 'ready'
                  ? 'text-bull'
                  : layers.daily.status === 'degraded'
                  ? 'text-amber-400'
                  : 'text-muted'
              }`}
            >
              {layers.daily.pct}%
            </span>
          </div>
          <div className="text-xs font-medium text-foreground">
            覆盖 {layers.daily.count} / {total_universe} 只
          </div>
          <div className="w-full bg-border/40 h-1.5 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${
                layers.daily.status === 'ready'
                  ? 'bg-bull'
                  : layers.daily.status === 'degraded'
                  ? 'bg-amber-400'
                  : 'bg-muted'
              }`}
              style={{ width: `${Math.min(100, Math.max(2, layers.daily.pct))}%` }}
            />
          </div>
        </div>

        {/* 4. 15:35 盘后定版与预选 */}
        <div className="p-3 rounded-xl bg-surface/60 border border-border/70 space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-secondary flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-teal-400" />
              15:35 盘后定版
            </span>
            {focus_stages.final_1535.status === 'ready' ? (
              <span className="text-[11px] text-teal-400 font-medium">已定版</span>
            ) : (
              <span className="text-[11px] text-muted">待定版</span>
            )}
          </div>
          <div className="text-xs font-medium text-foreground flex items-center gap-2">
            <span>正式: <strong className="text-bull font-mono">{focus_stages.final_1535.final_total ?? 0}</strong> 只</span>
            <span>·</span>
            <span>预选: <strong className="text-sky-400 font-mono">{focus_stages.final_1535.preselect_total ?? 0}</strong> 只</span>
          </div>
          <div className="text-[11px] text-muted">
            {layers.enriched.status === 'ready' ? 'Enriched 指标全量就绪' : '等待盘后管道计算指标'}
          </div>
        </div>
      </div>

      {/* 底层关键数据层健康诊断矩阵 */}
      {!compact && (
        <div className="pt-1">
          <div className="text-[11px] font-medium text-muted uppercase tracking-wider mb-2 flex items-center gap-1.5">
            <Database className="h-3 w-3 text-secondary" />
            全景底层数据层状态详情
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs">
            <div className="p-2.5 rounded-lg bg-surface/40 border border-border/50">
              <div className="text-muted text-[11px]">个股维表</div>
              <div className="font-semibold text-foreground font-mono mt-0.5">
                {layers.instruments.count} 只
              </div>
              <div className="text-[10px] text-bull mt-1">100% 完整</div>
            </div>

            <div className="p-2.5 rounded-lg bg-surface/40 border border-border/50">
              <div className="text-muted text-[11px]">全量分钟K</div>
              <div className="font-semibold text-foreground font-mono mt-0.5">
                {layers.minute.count} 只
              </div>
              <div className="text-[10px] text-secondary mt-1 truncate" title={layers.minute.max_time ?? ''}>
                {layers.minute.rows ? `${(layers.minute.rows / 10000).toFixed(0)}万条` : '暂无'}
                {layers.minute.max_time ? ` · ${layers.minute.max_time.slice(11, 16)}` : ''}
              </div>
            </div>

            <div className="p-2.5 rounded-lg bg-surface/40 border border-border/50">
              <div className="text-muted text-[11px]">全市场日K</div>
              <div className="font-semibold text-foreground font-mono mt-0.5">
                {layers.daily.count} 只
              </div>
              <div
                className={`text-[10px] mt-1 font-medium ${
                  layers.daily.status === 'ready'
                    ? 'text-bull'
                    : layers.daily.status === 'degraded'
                    ? 'text-amber-400 font-bold'
                    : 'text-muted'
                }`}
              >
                {layers.daily.status === 'ready'
                  ? '已完整生成'
                  : layers.daily.status === 'degraded'
                  ? `严重残缺 (${layers.daily.pct}%)`
                  : '未生成'}
              </div>
            </div>

            <div className="p-2.5 rounded-lg bg-surface/40 border border-border/50">
              <div className="text-muted text-[11px]">Enriched 指标</div>
              <div className="font-semibold text-foreground font-mono mt-0.5">
                {layers.enriched.count} 只
              </div>
              <div
                className={`text-[10px] mt-1 font-medium ${
                  layers.enriched.status === 'ready'
                    ? 'text-bull'
                    : layers.enriched.status === 'degraded'
                    ? 'text-amber-400 font-bold'
                    : 'text-muted'
                }`}
              >
                {layers.enriched.status === 'ready'
                  ? `${layers.enriched.fields} 维指标`
                  : layers.enriched.status === 'degraded'
                  ? `残缺 (${layers.enriched.pct}%)`
                  : '未计算'}
              </div>
            </div>

            <div className="p-2.5 rounded-lg bg-surface/40 border border-border/50">
              <div className="text-muted text-[11px]">竞价逐笔Tick</div>
              <div className="font-semibold text-foreground font-mono mt-0.5">
                {layers.quote_ticks.status === 'ready' ? '已入库' : '无数据'}
              </div>
              <div className="text-[10px] text-secondary mt-1">09:15-09:25 撮合</div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
