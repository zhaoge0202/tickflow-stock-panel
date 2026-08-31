import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Key,
  Eye,
  EyeOff,
  Trash2,
  CheckCircle2,
  AlertCircle,
  ExternalLink,
  Loader2,
  Save,
  Check,
  HelpCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useSettings } from '@/lib/useSharedQueries'
import { QK } from '@/lib/queryKeys'
import { tierStyle, tierBaseName, ALL_TIERS, TierTag } from '@/lib/capability-labels'

// ===== TickFlow 详情内嵌区块 (组合进数据源页的单一详情卡, 不再各自成卡) =====
// 档位变化会重塑能力矩阵候选 (按档位过滤) → Key/档位相关写操作统一连带失效。

/** 区块小标题 (详情卡内部的分节, 区别于页面级 section) */
function SectionHeading({ icon: Icon, title, badge, right }: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  badge?: string
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-2 mb-3">
      <div className="flex items-center gap-2 min-w-0">
        <Icon className="h-3.5 w-3.5 text-secondary shrink-0" />
        <h3 className="text-xs font-medium text-foreground">{title}</h3>
        {badge && (
          <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-elevated text-muted shrink-0">{badge}</span>
        )}
      </div>
      {right}
    </div>
  )
}

/** Key/档位/能力矩阵/侧栏状态的统一连带失效 (档位变化重塑矩阵候选) */
export function useInvalidateTierRelated() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: QK.settings })
    qc.invalidateQueries({ queryKey: QK.capabilities })
    qc.invalidateQueries({ queryKey: QK.capabilityMatrix })
    // 档位变化会改变实时行情模式(none/watchlist/full_market), 立即刷新侧边栏状态
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }
}

/** API Key 配置区块: 状态 + 输入 + 保存并检测 (先探后存) */
export function TickFlowKeySection({ right }: { right?: React.ReactNode }) {
  const settings = useSettings()
  const invalidate = useInvalidateTierRelated()

  const [keyInput, setKeyInput] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = useMutation({
    mutationFn: () => api.saveTickflowKey(keyInput.trim()),
    onSuccess: (data) => {
      setKeyInput('')
      invalidate()
      if (data.ok) {
        setSaved(true)
        setTimeout(() => setSaved(false), 2000)
      }
      // ok=false 由 save.data 在下方渲染提示(reason=invalid),无需额外处理
    },
  })

  const clear = useMutation({
    mutationFn: () => api.clearTickflowKey(),
    onSuccess: () => invalidate(),
  })

  const redetect = useMutation({
    mutationFn: () => api.redetectCapabilities(),
    onSuccess: () => invalidate(),
  })

  const mode = settings.data?.mode
  const masked = settings.data?.tickflow_api_key_masked

  return (
    <div>
      <SectionHeading icon={Key} title="TickFlow API Key" right={right} />

      <p className="text-xs text-secondary leading-relaxed mb-4">
        在{' '}
        <a
          href="https://tickflow.org/auth/register?ref=V3KDKGXPEA"
          target="_blank"
          rel="noreferrer"
          className="text-accent hover:underline inline-flex items-baseline gap-0.5"
        >
          tickflow.org
          <ExternalLink className="h-3 w-3 self-center" />
        </a>{' '}
        注册获取。API Key 存放为本地文件,不会上传任何第三方,请妥善保管。
      </p>

      {/* 当前状态 */}
      <div className="flex items-center justify-between mb-4">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-widest text-muted">状态</div>
          <div className="mt-1 flex items-center gap-2 min-w-0">
            {mode === 'api_key' ? (
              <>
                <CheckCircle2 className="h-4 w-4 text-bear shrink-0" />
                <span className="text-sm font-medium shrink-0">已配置</span>
                <span className="font-mono text-xs text-secondary truncate">{masked}</span>
              </>
            ) : mode === 'free' ? (
              <>
                <CheckCircle2 className="h-4 w-4 text-bear shrink-0" />
                <span className="text-sm font-medium shrink-0">免费 Key</span>
                <span className="font-mono text-xs text-secondary truncate">{masked}</span>
              </>
            ) : (
              <>
                <AlertCircle className="h-4 w-4 text-muted shrink-0" />
                <span className="text-sm font-medium text-muted">未配置</span>
              </>
            )}
          </div>
        </div>
        {(mode === 'api_key' || mode === 'free') && (
          <button
            onClick={() => setConfirmClear(true)}
            disabled={clear.isPending}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn bg-elevated text-secondary hover:text-danger text-xs transition-colors duration-150 ease-smooth disabled:opacity-50 shrink-0"
          >
            <Trash2 className="h-3 w-3" />
            清除
          </button>
        )}
      </div>

      {/* 输入 + 保存: 左右一行 */}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (keyInput.trim()) save.mutate()
        }}
        className="flex items-center gap-2"
      >
        <div className="relative flex-1 min-w-0">
          <input
            type={revealing ? 'text' : 'password'}
            placeholder={mode === 'none' ? '粘贴 TickFlow API Key' : '粘贴新 Key 替换当前'}
            value={keyInput}
            onChange={(e) => { setKeyInput(e.target.value); if (saved) setSaved(false) }}
            className="w-full px-3 py-2 pr-9 rounded-input bg-base border border-border text-sm font-mono focus:outline-none focus:border-accent transition-colors duration-150 ease-smooth"
          />
          <button
            type="button"
            onClick={() => setRevealing((v) => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-foreground transition-colors duration-150 ease-smooth"
            tabIndex={-1}
            aria-label={revealing ? '隐藏' : '显示'}
          >
            {revealing ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
        <button
          type="submit"
          disabled={save.isPending || (!keyInput.trim() && !saved)}
          className="h-9 shrink-0 px-4 rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all"
        >
          {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
          {save.isPending ? '保存中...' : saved ? '已保存' : '保存并检测'}
        </button>
      </form>

      {/* 检测中提示 —— 成功/失败后自动消失 */}
      {save.isPending && (
        <div className="mt-2 flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] leading-snug text-warning">
          <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
            <span>
              验证通过前请不要离开当前页面 · 如遇网络问题请点击
              <button
                type="button"
                onClick={() => { save.reset(); redetect.mutate() }}
                disabled={redetect.isPending}
                className="font-semibold underline underline-offset-2 hover:text-warning/80 disabled:opacity-50"
              >
                {redetect.isPending ? '重新检测中…' : '重新检测'}
              </button>
            </span>
          </div>
        )}

      {save.isError && (
        <div className="mt-3 text-xs text-danger">
          保存失败:{String((save.error as any).message)}
        </div>
      )}
      {/* 无效 key —— 先探后存:探测失败(key 无效/乱填)时不存储,提示用户 */}
      {save.data && !save.data.ok && (
        <div className="mt-3 text-xs text-danger flex items-center gap-1.5">
          <AlertCircle className="h-3 w-3 shrink-0" />
          {save.data.reason === 'invalid'
            ? 'Key 无效或已过期,请检查后重试(未保存该 Key)'
            : save.data.error ?? '保存失败'}
        </div>
      )}
      {save.data?.ok && (
        <div className="mt-3 text-xs text-bear flex items-center gap-1.5">
          <CheckCircle2 className="h-3 w-3" />
          保存成功 — 档位 {save.data.tier_label}
          {save.data.mode === 'free' && '(免费档 · 历史日K + 自选实时监控)'}
        </div>
      )}

      {/* 确认清除 Key 弹窗 */}
      {confirmClear && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setConfirmClear(false)}
          />
          <div className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6">
            <h3 className="text-sm font-medium text-foreground mb-2">清除 API Key</h3>
            <p className="text-xs text-secondary mb-5">
              清除后将退回 None 档(仅历史日K),需要重新输入 Key 才能恢复。
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setConfirmClear(false)}
                className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => { setConfirmClear(false); clear.mutate() }}
                disabled={clear.isPending}
                className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50"
              >
                {clear.isPending ? '清除中...' : '确认清除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ===== 档位说明弹窗 =====

export function TierHelpPopover({ currentLabel }: { currentLabel: string }) {
  const [open, setOpen] = useState(false)
  const currentBase = tierBaseName(currentLabel)

  return (
    <div className="relative inline-flex items-center">
      <HelpCircle
        className="h-4 w-4 text-muted/60 cursor-help hover:text-muted transition-colors"
        onClick={() => setOpen(v => !v)}
      />
      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, y: -4, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.95 }}
              transition={{ duration: 0.15 }}
              className="absolute top-full left-0 mt-1 z-50 w-72 bg-surface border border-border rounded-lg shadow-xl p-3.5 text-[11px] leading-relaxed"
              onClick={e => e.stopPropagation()}
            >
              {/* 档位 tag 横排 */}
              <div className="flex items-center gap-1.5 mb-3">
                {ALL_TIERS.map(t => (
                  <div key={t} className={`flex flex-col items-center gap-1 ${t === currentBase ? '' : 'opacity-60'}`}>
                    <TierTag label={t} />
                  </div>
                ))}
              </div>

              {/* 每档说明 */}
              <div className="space-y-1 mb-3 pb-3 border-b border-border">
                {ALL_TIERS.map(t => {
                  const s = tierStyle(t)
                  return (
                    <div key={t} className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full shrink-0" style={s.dotStyle} />
                      <span className="font-mono font-bold w-12 shrink-0" style={s.labelTextStyle}>{t === 'none' ? 'None' : t}</span>
                      <span className="text-secondary">{s.desc}</span>
                    </div>
                  )
                })}
              </div>

              <div className="mb-3 rounded-btn border border-warning/30 bg-warning/10 px-2.5 py-1.5 text-[11px] font-medium text-warning">
                高等档位包含较低档位的全部权益。
              </div>

              {/* 检测说明 */}
              <div className="text-secondary space-y-1.5">
                <div className="font-medium text-foreground">档位检测说明</div>
                <p>保存 Key 后系统会在付费端点逐一试探数据能力:连单只日K都拿不到则判为「None」(不存 Key);有日K但无复权因子则判为「Free」;有复权因子再按代表能力判定 Starter/Pro/Expert。</p>
                <p className="text-muted">None 档与 Free 档运行时都走免费数据通道(仅历史日K),区别仅在于是否保存了 Key。付费档走付费端点,享有实时行情等完整能力。</p>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}
