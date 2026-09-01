import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { AlertTriangle, Info } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

// 无除权因子能力时的同步前置确认。
// 背景: 除权因子不可用时同步静默降级 —— 日K按不复权价入库、enriched 直接用
// raw 价算指标 (pipeline 契约), 后续发生除权事件已有数据需重新校准。
// 该降级无任何运行期提示, 故在同步入口处前置拦截告知一次。

const NO_REMIND_KEY = 'tsp_adj_factor_no_remind'

/**
 * 同步入口的除权因子门控。
 * guard(run): 除权因子能力可用 (或矩阵未加载完成 fail-open / 用户已选不再提示) 时直接执行,
 * 否则弹二次确认; dialog 需渲染在调用方页面根部。
 */
export function useAdjFactorSyncGate() {
  const matrix = useQuery({
    queryKey: QK.capabilityMatrix,
    queryFn: api.capabilityMatrix,
    staleTime: 60_000,
  })
  const [pending, setPending] = useState<{ run: () => void } | null>(null)
  const [noRemind, setNoRemind] = useState(() => {
    try { return localStorage.getItem(NO_REMIND_KEY) === '1' } catch { return false }
  })

  const adjRoute = matrix.data?.capabilities.find(c => c.id === 'adj_factor')
  // 矩阵未加载完成时不拦截 (fail-open): 首次同步流程不被慢请求卡住
  const adjUsable = matrix.isLoading || !matrix.data ? true : (adjRoute?.usable ?? true)

  const guard = (run: () => void) => {
    if (adjUsable || noRemind) { run(); return }
    setPending({ run })
  }

  const dialog = pending ? (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={() => setPending(null)}
      />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative w-[90vw] max-w-[420px] rounded-card border border-border bg-base shadow-2xl p-6"
      >
        <div className="flex items-start gap-3">
          <div className="shrink-0 h-10 w-10 rounded-full bg-warning/12 flex items-center justify-center">
            <AlertTriangle className="h-5 w-5 text-warning" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-foreground mb-1.5">当前无除权因子能力</h3>
            <p className="text-xs text-secondary leading-relaxed">
              本次同步的日K将以<span className="text-foreground font-medium">不复权价格</span>入库并计算指标,
              K 线可能包含除权跳空。后续配置除权因子能力后重新同步, 才能得到前复权口径的数据。
            </p>
            <div className="mt-2 flex items-start gap-1.5 text-[11px] text-warning">
              <Info className="h-3.5 w-3.5 shrink-0 mt-px text-warning" />
              <span>若期间发生除权(分红/送转)事件, 已有数据需要重新校准。</span>
            </div>
            <label className="mt-3 flex items-center gap-1.5 text-[11px] text-muted cursor-pointer select-none">
              <input
                type="checkbox"
                checked={noRemind}
                onChange={e => setNoRemind(e.target.checked)}
                className="h-3 w-3 accent-accent cursor-pointer"
              />
              不再提示
            </label>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={() => setPending(null)}
            className="px-4 h-9 rounded-btn text-sm text-secondary hover:text-foreground hover:bg-elevated transition-colors"
          >
            取消
          </button>
          <button
            onClick={() => {
              try { localStorage.setItem(NO_REMIND_KEY, noRemind ? '1' : '0') } catch { /* 私隐模式等场景静默 */ }
              pending.run()
              setPending(null)
            }}
            className="px-4 h-9 rounded-btn bg-accent text-white text-sm font-medium hover:bg-accent/90 transition-colors"
          >
            仍然同步
          </button>
        </div>
      </motion.div>
    </div>
  ) : null

  return { guard, dialog }
}
