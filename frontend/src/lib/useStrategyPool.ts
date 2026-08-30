import { useState, useCallback, useEffect } from 'react'
import { storage } from '@/lib/storage'

export type StrategyTimeframe = '1d' | '1m'

// 两周期各自独立持久化, 互不可见: 日线池只装日线策略, 分钟池只装分钟策略。
// 切换周期时 pool 指向对应列表, 任何写操作都只会落到所属 key。
const POOL_STORES: Record<StrategyTimeframe, typeof storage.strategyPool> = {
  '1d': storage.strategyPool,
  '1m': storage.strategyPoolMinute,
}

/**
 * 策略池 — 按周期 (日线/分钟) 隔离的两份池。
 * pool 为当前周期的列表; addToPool 可显式指定目标周期
 * (如策略构建器创建的是日线策略, 即使在分钟页保存也应入日线池)。
 */
export function useStrategyPool(timeframe: StrategyTimeframe = '1d') {
  const [pools, setPools] = useState<Record<StrategyTimeframe, string[]>>(() => ({
    '1d': storage.strategyPool.get([]),
    '1m': storage.strategyPoolMinute.get([]),
  }))
  const pool = pools[timeframe]

  // 任一池变化即整体落库 (两份 key 幂等重写, 避免"只写当前池"漏掉非活跃池的更新)
  useEffect(() => {
    POOL_STORES['1d'].set(pools['1d'])
    POOL_STORES['1m'].set(pools['1m'])
  }, [pools])

  const mutate = useCallback(
    (tf: StrategyTimeframe, updater: (prev: string[]) => string[]) => {
      setPools(prev => ({ ...prev, [tf]: updater(prev[tf]) }))
    },
    [],
  )

  const addToPool = useCallback((id: string, tf: StrategyTimeframe = timeframe) => {
    mutate(tf, prev => (prev.includes(id) ? prev : [...prev, id]))
  }, [mutate, timeframe])

  const removeFromPool = useCallback((id: string) => {
    mutate(timeframe, prev => prev.filter(x => x !== id))
  }, [mutate, timeframe])

  const reorderPool = useCallback((newOrder: string[]) => {
    mutate(timeframe, () => newOrder)
  }, [mutate, timeframe])

  // 清除池中不存在于 validIds 的失效策略(如本地开发残留的自定义策略)。
  // 调用方需以"当前周期自己的策略列表"传入, 各周期只清理各自的池。
  // 仅当确实有失效项时才更新,避免无谓重渲染。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    mutate(timeframe, prev => {
      if (prev.length === 0) return prev
      const next = prev.filter(id => validSet.has(id))
      return next.length === prev.length ? prev : next
    })
  }, [mutate, timeframe])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, pools, addToPool, removeFromPool, reorderPool, prune, isInPool }
}
