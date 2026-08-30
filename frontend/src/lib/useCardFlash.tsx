import { useEffect, useRef, useState } from 'react'

/**
 * 设置卡片定位闪烁锚点。
 *
 * 用法: /settings?tab=monitoring&highlight=<key> 打开时, 声明了 anchor=key 的
 * 卡片滚动到视口中央并 ring 高亮 2 秒 — 从其他页面引导用户"去某张卡片开启/配置"
 * 时精确定位, 不再让用户在设置页里大海捞针。
 *
 * 每个锚点 key 一次会话只触发一次 (firedRef), 避免 StrictMode 双渲染重复闪烁。
 */
export function useCardFlash(highlight: string | undefined, key: string) {
  const ref = useRef<HTMLDivElement>(null)
  const firedRef = useRef(false)
  const [flash, setFlash] = useState(false)

  useEffect(() => {
    if (highlight !== key || firedRef.current || !ref.current) return
    firedRef.current = true
    ref.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    // 等滚动起一帧再点亮, 视觉上"滚过去 → 闪一下"更连贯
    requestAnimationFrame(() => {
      setFlash(true)
      window.setTimeout(() => setFlash(false), 2000)
    })
  }, [highlight, key])

  return { ref, flash }
}

/** 闪烁态样式: 与连板梯队修正卡片 (depth-fix) 原实现保持一致 */
export function cardFlashCls(flash: boolean): string {
  return `rounded-card transition-all duration-500 ${
    flash ? 'ring-2 ring-accent/60 ring-offset-2 ring-offset-base scale-[1.01]' : 'ring-0 ring-transparent'
  }`
}

/** 锚点包裹层: 面板没有统一 Card 组件时, 直接包住目标区块即可 */
export function AnchorWrap({
  highlight,
  anchor,
  children,
}: {
  highlight?: string
  anchor: string
  children: React.ReactNode
}) {
  const { ref, flash } = useCardFlash(highlight, anchor)
  return (
    <div ref={ref} id={anchor} className={cardFlashCls(flash)}>
      {children}
    </div>
  )
}
