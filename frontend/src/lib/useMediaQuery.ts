import { useSyncExternalStore } from 'react'

/**
 * 响应式媒体查询 hook — 视口变化触发重渲染, 服务器端渲染恒返回 false。
 * 侧边栏用它区分桌面三态 (展开/图标条/隐藏) 与移动端抽屉两种交互模型。
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    () => window.matchMedia(query).matches,
    () => false,
  )
}

/** ≥768px 视口 (桌面布局)。 */
export function useIsDesktop(): boolean {
  return useMediaQuery('(min-width: 768px)')
}
