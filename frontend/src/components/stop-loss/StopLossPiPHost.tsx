import React, { useEffect, useRef, useState, useCallback } from 'react'
import { createPortal } from 'react-dom'
import {
  useMonitoredPositions,
  useIsPipOpen,
  setPipOpen,
  removeMonitoredPosition,
  addOrUpdateMonitoredPosition,
  updateMonitoredQuotes,
  type MonitoredPosition,
} from '@/lib/stopLossStore'
import { calculateDynamicStopLoss, type StopLossCalculation } from '@/lib/dynamicStopLoss'
import { api } from '@/lib/api'
import { fmtPrice, fmtPct } from '@/lib/format'
import { cn } from '@/lib/cn'
import { toast } from '@/components/Toast'
import {
  ShieldAlert,
  ShieldCheck,
  TrendingUp,
  TrendingDown,
  X,
  ExternalLink,
  Minimize2,
  Maximize2,
  Trash2,
  Edit2,
  ArrowUpRight,
  Plus,
  Target,
  Loader2,
  Tv,
  RotateCcw,
} from 'lucide-react'

// ===== 检查浏览器原生 Document Picture-in-Picture 支持 =====
function isDocumentPipSupported(): boolean {
  return typeof window !== 'undefined' && 'documentPictureInPicture' in window
}

/** 规范化输入的股票代码 (如 600354 -> 600354.SH) */
function normalizeInputSymbol(code: string): string {
  const trimmed = code.trim().toUpperCase()
  if (trimmed.includes('.')) return trimmed
  if (trimmed.startsWith('6') || trimmed.startsWith('9')) {
    return `${trimmed}.SH`
  }
  if (trimmed.startsWith('0') || trimmed.startsWith('3')) {
    return `${trimmed}.SZ`
  }
  if (trimmed.startsWith('8') || trimmed.startsWith('4')) {
    return `${trimmed}.BJ`
  }
  return trimmed
}

// ===== Canvas 动态绘制函数: 用于驱动 100% 绝对置顶的视频画中画 =====
function renderStopLossCanvas(
  canvas: HTMLCanvasElement,
  positions: MonitoredPosition[],
) {
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const dpr = 2
  const logicalWidth = 360
  const logicalHeight = Math.max(160, 48 + Math.max(1, positions.length) * 96)

  if (canvas.width !== logicalWidth * dpr || canvas.height !== logicalHeight * dpr) {
    canvas.width = logicalWidth * dpr
    canvas.height = logicalHeight * dpr
  }

  ctx.save()
  ctx.scale(dpr, dpr)

  // 背景: 沉浸式高级深色背景 #090D16
  ctx.fillStyle = '#090D16'
  ctx.fillRect(0, 0, logicalWidth, logicalHeight)

  // 顶栏: 标题 + 实时时间
  ctx.fillStyle = '#38BDF8'
  ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
  ctx.fillText('🎯 动态止损实时置顶盯盘', 12, 22)

  const timeStr = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  ctx.fillStyle = '#64748B'
  ctx.font = '10px monospace'
  ctx.textAlign = 'right'
  ctx.fillText(`${timeStr} · ${positions.length}只`, logicalWidth - 12, 22)
  ctx.textAlign = 'left'

  // 分割线
  ctx.strokeStyle = '#1E293B'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(10, 32)
  ctx.lineTo(logicalWidth - 10, 32)
  ctx.stroke()

  if (positions.length === 0) {
    ctx.fillStyle = '#94A3B8'
    ctx.font = '12px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText('暂无盯盘标的，请在主页面添加', logicalWidth / 2, logicalHeight / 2)
    ctx.restore()
    return
  }

  // 绘制每个标的卡片
  let y = 42
  for (const item of positions) {
    const cur = item.currentPrice || item.costPrice
    const calc = calculateDynamicStopLoss({
      costPrice: item.costPrice,
      currentPrice: cur,
      peakPrice: item.peakPrice,
      ma5: item.ma5,
      holdingDays: item.holdingDays,
    })

    const isTriggered = calc.state === 'TRIGGERED'
    const isWarning = calc.state === 'WARNING'
    const changePct = item.changePct ?? 0
    const isBull = changePct >= 0

    // 卡片背景
    ctx.fillStyle = isTriggered
      ? 'rgba(239, 68, 68, 0.2)'
      : isWarning
        ? 'rgba(234, 179, 8, 0.15)'
        : '#131C2E'
    ctx.strokeStyle = isTriggered ? '#EF4444' : isWarning ? '#EAB308' : '#1E293B'
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.roundRect(10, y, logicalWidth - 20, 88, 8)
    ctx.fill()
    ctx.stroke()

    // 股票名称与代码
    ctx.fillStyle = '#FFFFFF'
    ctx.font = 'bold 14px sans-serif'
    ctx.fillText(item.name, 20, y + 22)

    ctx.fillStyle = '#94A3B8'
    ctx.font = '10px monospace'
    ctx.fillText(item.symbol, 20 + ctx.measureText(item.name).width + 6, y + 21)

    // 现价与涨跌幅
    const priceStr = cur.toFixed(2)
    const pctStr = `${isBull ? '+' : ''}${(changePct * 100).toFixed(2)}%`
    ctx.fillStyle = isBull ? '#EF4444' : '#10B981'
    ctx.font = 'bold 15px monospace'
    ctx.textAlign = 'right'
    ctx.fillText(priceStr, logicalWidth - 78, y + 22)

    ctx.font = 'bold 11px monospace'
    ctx.fillText(pctStr, logicalWidth - 18, y + 22)
    ctx.textAlign = 'left'

    // 成本 vs 最高 Peak
    ctx.fillStyle = '#94A3B8'
    ctx.font = '11px sans-serif'
    ctx.fillText(`成本 ${item.costPrice.toFixed(2)}`, 20, y + 42)
    ctx.fillText(`最高Peak ${calc.peakPrice.toFixed(2)}`, 115, y + 42)

    // 核心出场防守线
    ctx.fillStyle = isTriggered ? '#F87171' : isWarning ? '#FDE047' : '#F8FAFC'
    ctx.font = 'bold 11px sans-serif'
    ctx.fillText(`出场线: ${calc.effectiveStopPrice.toFixed(2)}`, 215, y + 42)

    // 安全垫进度条与状态
    const barWidth = logicalWidth - 40
    const barHeight = 5
    const barY = y + 52

    ctx.fillStyle = '#1E293B'
    ctx.beginPath()
    ctx.roundRect(20, barY, barWidth, barHeight, 2.5)
    ctx.fill()

    const fillRatio = isTriggered ? 1 : Math.max(0.05, Math.min(1, calc.safetyMarginPct / 5))
    ctx.fillStyle = isTriggered ? '#EF4444' : isWarning ? '#EAB308' : '#10B981'
    ctx.beginPath()
    ctx.roundRect(20, barY, barWidth * fillRatio, barHeight, 2.5)
    ctx.fill()

    // 状态文字
    ctx.fillStyle = isTriggered ? '#EF4444' : isWarning ? '#EAB308' : '#10B981'
    ctx.font = 'bold 10px sans-serif'
    const statusText = isTriggered
      ? '🚨 触及出场线，建议立即离场！'
      : isWarning
        ? `⚠️ 安全垫仅剩 +${calc.safetyMarginPct}% (预警)`
        : `安全垫 +${calc.safetyMarginPct}% · ${calc.trailingLabel} ${calc.trailingStopPrice.toFixed(2)}`
    ctx.fillText(statusText, 20, y + 74)

    // 盈亏
    const pnlStr = `盈亏: ${calc.pnlPct >= 0 ? '+' : ''}${calc.pnlPct}%`
    ctx.fillStyle = calc.pnlPct >= 0 ? '#EF4444' : '#10B981'
    ctx.textAlign = 'right'
    ctx.font = '10px monospace'
    ctx.fillText(pnlStr, logicalWidth - 20, y + 74)
    ctx.textAlign = 'left'

    y += 94
  }

  ctx.restore()
}

export function StopLossPiPHost() {
  const positions = useMonitoredPositions()
  const isPipOpen = useIsPipOpen()

  const [pipWindow, setPipWindow] = useState<Window | null>(null)
  const [minimized, setMinimized] = useState(false)
  const [editingSymbol, setEditingSymbol] = useState<string | null>(null)
  const [editingPrice, setEditingPrice] = useState<string>('')

  // 视频画中画专用元素引用
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [isVideoPipActive, setIsVideoPipActive] = useState(false)

  // 添加新标的表单状态
  const [showAddForm, setShowAddForm] = useState(false)
  const [newSymbolInput, setNewSymbolInput] = useState('')
  const [newCostInput, setNewCostInput] = useState('')
  const [addingLoading, setAddingLoading] = useState(false)
  const [addError, setAddError] = useState('')

  // 拖拽坐标 (站内浮窗模式)
  const [pos, setPos] = useState({ x: 24, y: 72 })
  const draggingRef = useRef(false)
  const dragStartRef = useRef({ mx: 0, my: 0, ox: 0, oy: 0 })

  // ===== 0. 自动补齐股票名称 (解决历史数据或快捷标记只有代码没有名称的问题) =====
  useEffect(() => {
    const needNameSymbols = positions
      .filter((p) => !p.name || p.name === p.symbol || p.name.toUpperCase().includes('.SZ') || p.name.toUpperCase().includes('.SH') || p.name.toUpperCase().includes('.BJ'))
      .map((p) => p.symbol)

    if (needNameSymbols.length === 0) return

    let active = true
    api.instrumentNames(needNameSymbols)
      .then((res) => {
        if (!active || !res?.names) return
        for (const [sym, realName] of Object.entries(res.names)) {
          if (realName && realName !== sym) {
            const cur = positions.find((p) => p.symbol.toUpperCase() === sym.toUpperCase())
            if (cur && cur.name !== realName) {
              addOrUpdateMonitoredPosition({
                ...cur,
                name: realName,
              })
            }
          }
        }
      })
      .catch(() => {})

    return () => {
      active = false
    }
  }, [positions])

  // ===== 1. 实时行情高频轮询 (每 2.5 秒更新一次盯盘标的) =====
  useEffect(() => {
    if (!isPipOpen || positions.length === 0) return

    let active = true
    const fetchLatest = async () => {
      try {
        const symbols = positions.map((p) => p.symbol)
        const updates: Record<
          string,
          { currentPrice: number; high?: number; changePct?: number; ma5?: number | null }
        > = {}

        await Promise.all(
          symbols.map(async (sym) => {
            try {
              const res = await api.klineDaily(sym, 10)
              const rows = res.rows || []
              if (rows.length > 0) {
                const latest = rows[rows.length - 1]
                const cur = Number(latest.close)
                const high = Number(latest.high)
                const prevClose = Number(latest.prev_close || latest.close)
                const changePct = prevClose > 0 ? (cur - prevClose) / prevClose : 0
                const ma5 = latest.ma5 != null ? Number(latest.ma5) : null

                updates[sym] = {
                  currentPrice: cur,
                  high,
                  changePct,
                  ma5,
                }
              }
            } catch {
              /* ignore single fetch error */
            }
          }),
        )

        if (active && Object.keys(updates).length > 0) {
          updateMonitoredQuotes(updates)
        }
      } catch {
        /* ignore */
      }
    }

    void fetchLatest()
    const timer = setInterval(() => {
      void fetchLatest()
    }, 2500)

    return () => {
      active = false
      clearInterval(timer)
    }
  }, [isPipOpen, positions])

  // ===== 2. 视频画中画后台预热 (常驻建立媒体流，确保 metadata 早就就绪，绝不抛 InvalidStateError) =====
  useEffect(() => {
    if (!canvasRef.current) {
      canvasRef.current = document.createElement('canvas')
    }
    renderStopLossCanvas(canvasRef.current, positions)

    let v = videoRef.current
    if (!v) {
      v = document.createElement('video')
      v.muted = true
      v.playsInline = true
      v.autoplay = true
      v.style.position = 'fixed'
      v.style.width = '1px'
      v.style.height = '1px'
      v.style.opacity = '0.001'
      v.style.pointerEvents = 'none'
      v.style.zIndex = '-9999'
      document.body.appendChild(v)

      const stream = (canvasRef.current as any).captureStream
        ? (canvasRef.current as any).captureStream(2)
        : null
      if (stream) {
        v.srcObject = stream
        v.play().catch(() => {})
      }
      videoRef.current = v
      v.addEventListener('leavepictureinpicture', () => {
        setIsVideoPipActive(false)
      })
    } else if (v.paused) {
      v.play().catch(() => {})
    }
  }, [positions])

  // ===== 3. DOM 原生画中画 / 独立小窗管理 =====
  const copyStylesToWindow = (targetWin: Window) => {
    Array.from(document.styleSheets).forEach((styleSheet) => {
      try {
        if (styleSheet.href) {
          const link = targetWin.document.createElement('link')
          link.rel = 'stylesheet'
          link.type = styleSheet.type
          link.media = styleSheet.media.mediaText || 'all'
          link.href = styleSheet.href
          targetWin.document.head.appendChild(link)
        } else if (styleSheet.cssRules) {
          const style = targetWin.document.createElement('style')
          Array.from(styleSheet.cssRules).forEach((rule) => {
            style.appendChild(targetWin.document.createTextNode(rule.cssText))
          })
          targetWin.document.head.appendChild(style)
        }
      } catch {
        /* ignore cross-origin stylesheets */
      }
    })

    if (document.documentElement.classList.contains('dark')) {
      targetWin.document.documentElement.classList.add('dark')
    }
    targetWin.document.title = '🎯 动态止损盯盘'
    targetWin.document.body.className = 'bg-base text-foreground font-sans p-3 select-none overflow-y-auto'
  }

  const openFloatingWindow = useCallback(async () => {
    const targetHeight = Math.min(640, Math.max(260, 180 + positions.length * 135))

    // 1. 优先尝试现代浏览器原生 Document Picture-in-Picture
    if (isDocumentPipSupported()) {
      try {
        // @ts-expect-error - documentPictureInPicture 是现代浏览器特性
        const pip = await window.documentPictureInPicture.requestWindow({
          width: 360,
          height: targetHeight,
        })
        copyStylesToWindow(pip)
        pip.addEventListener('pagehide', () => setPipWindow(null))
        setPipWindow(pip)
        toast('✅ 原生交互式画中画已启动！', 'success')
        return
      } catch (err) {
        console.warn('原生 Document PiP 开启失败，回退至独立小窗:', err)
      }
    }

    // 2. 局域网 HTTP / 普通浏览器环境 100% 兼容方案: window.open 独立小窗
    try {
      const left = Math.max(10, (window.screen?.availWidth || 1200) - 400)
      const top = 100
      const popup = window.open(
        '',
        'tf_pip_stop_loss',
        `width=360,height=${targetHeight},left=${left},top=${top},menubar=no,toolbar=no,location=no,status=no,resizable=yes`,
      )
      if (popup) {
        popup.document.write('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>🎯 动态止损盯盘</title></head><body></body></html>')
        popup.document.close()
        copyStylesToWindow(popup)
        popup.addEventListener('beforeunload', () => setPipWindow(null))
        setPipWindow(popup)
      }
    } catch (err) {
      console.error('打开独立小窗失败:', err)
    }
  }, [positions.length])

  // ===== 4. 终极置顶: 智能选择最强系统级画中画 (100% 任何外部软件无法遮挡) =====
  const handleToggleSystemPip = useCallback(async () => {
    // 1. 若当前已处于置顶状态，点击直接退出
    if (pipWindow) {
      pipWindow.close()
      setPipWindow(null)
      toast('已退出置顶盯盘')
      return
    }
    if (document.pictureInPictureElement) {
      await document.exitPictureInPicture().catch(() => {})
      setIsVideoPipActive(false)
      toast('已退出置顶画中画')
      return
    }

    // 2. 优先策略: 现代浏览器原生 Document Picture-in-Picture (支持完整鼠标/键盘交互，绝对系统置顶)
    if (isDocumentPipSupported()) {
      try {
        await openFloatingWindow()
        return
      } catch (err) {
        console.warn('Document PiP 调起失败，自动回退至视频画中画:', err)
      }
    }

    // 3. 兼容策略: 视频画中画 (Video PiP，全浏览器 100% 绝对置顶，同花顺等绝不遮挡)
    try {
      const v = videoRef.current
      if (!v) {
        throw new Error('画中画组件未就绪，请稍候重试')
      }

      // 确保至少播放并且 metadata 就绪
      if (v.readyState < 1) {
        v.play().catch(() => {})
        await new Promise<void>((resolve) => {
          v.onloadedmetadata = () => resolve()
          setTimeout(resolve, 200)
        })
      } else if (v.paused) {
        v.play().catch(() => {})
      }

      await v.requestPictureInPicture()
      setIsVideoPipActive(true)
      toast('🎯 操作系统级置顶画中画已启动！同花顺/通达信绝对无法遮挡', 'success')
    } catch (err: any) {
      console.warn('视频画中画开启失败，回退至独立小窗:', err)
      openFloatingWindow()
    }
  }, [pipWindow, openFloatingWindow])

  // ===== 5. 手动添加标的逻辑 =====
  const handleAddNewSymbol = async () => {
    const raw = newSymbolInput.trim()
    if (!raw) return
    const symbol = normalizeInputSymbol(raw)
    setAddingLoading(true)
    setAddError('')
    try {
      const res = await api.klineDaily(symbol, 10)
      const rows = res.rows || []
      const latest = rows[rows.length - 1]
      const cur = latest ? Number(latest.close) : 10
      const cost = parseFloat(newCostInput) > 0 ? parseFloat(newCostInput) : cur
      let name = res.name
      if (!name || name === symbol) {
        try {
          const namesResp = await api.instrumentNames([symbol])
          name = namesResp?.names?.[symbol] || symbol
        } catch {
          name = symbol
        }
      }

      addOrUpdateMonitoredPosition({
        symbol,
        name,
        costPrice: cost,
        currentPrice: cur,
        todayHigh: latest ? Number(latest.high) : cur,
        ma5: latest?.ma5 != null ? Number(latest.ma5) : null,
      })

      setNewSymbolInput('')
      setNewCostInput('')
      setShowAddForm(false)
    } catch (err: any) {
      setAddError(err.message || '获取标的失败，请检查代码')
    } finally {
      setAddingLoading(false)
    }
  }

  // ===== 6. 顶栏拖拽逻辑 (兼容站内浮窗 + 独立桌面小窗移动) =====
  const onTitlebarPointerDown = (e: React.PointerEvent) => {
    // 关键点: 点击任何按钮、输入框、标签时绝不触发拖拽，确保所有按钮 100% 灵敏响应
    if ((e.target as HTMLElement).closest('button, input, a, [role="button"]')) return

    draggingRef.current = true

    if (pipWindow) {
      // 桌面独立小窗模式: 基于屏幕绝对坐标移动独立小窗
      dragStartRef.current = {
        mx: e.screenX,
        my: e.screenY,
        ox: pipWindow.screenX,
        oy: pipWindow.screenY,
      }
    } else {
      // 站内浮窗卡片模式: 基于视口相对坐标移动 DOM
      dragStartRef.current = {
        mx: e.clientX,
        my: e.clientY,
        ox: pos.x,
        oy: pos.y,
      }
    }

    try {
      ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }

  const onTitlebarPointerMove = (e: React.PointerEvent) => {
    if (!draggingRef.current) return

    if (pipWindow) {
      // 移动桌面独立窗口
      const dx = e.screenX - dragStartRef.current.mx
      const dy = e.screenY - dragStartRef.current.my
      pipWindow.moveTo(dragStartRef.current.ox + dx, dragStartRef.current.oy + dy)
    } else {
      // 移动站内卡片
      const dx = e.clientX - dragStartRef.current.mx
      const dy = e.clientY - dragStartRef.current.my
      setPos({
        x: Math.max(10, Math.min(window.innerWidth - 380, dragStartRef.current.ox + dx)),
        y: Math.max(10, Math.min(window.innerHeight - 300, dragStartRef.current.oy + dy)),
      })
    }
  }

  const onTitlebarPointerUp = (e: React.PointerEvent) => {
    draggingRef.current = false
    try {
      ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }

  if (!isPipOpen) {
    return null
  }

  // 内部浮窗内容
  const content = (
    <div className="flex flex-col gap-2.5 w-full">
      {/* 顶栏控制条 (专属拖拽手柄: 按住本栏可在桌面上/页面内任意拖动) */}
      <div
        onPointerDown={onTitlebarPointerDown}
        onPointerMove={onTitlebarPointerMove}
        onPointerUp={onTitlebarPointerUp}
        className="flex items-center justify-between border-b border-border/60 pb-2 px-1 cursor-move select-none"
      >
        <div className="flex items-center gap-1.5">
          <div className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-bull opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-bull" />
          </div>
          <span className="text-xs font-semibold text-foreground tracking-wide flex items-center gap-1">
            <Target className="h-3.5 w-3.5 text-bull" />
            动态止损盯盘
          </span>
          <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-mono font-medium text-accent">
            {positions.length} 只
          </span>
        </div>

        <div className="flex items-center gap-1 text-muted">
          {/* 按钮 1: 真正操作系统级永远置顶小窗 (同花顺/通达信绝对无法遮挡) */}
          <button
            type="button"
            onClick={handleToggleSystemPip}
            className={cn(
              'flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium transition-colors cursor-pointer border',
              pipWindow || isVideoPipActive
                ? 'bg-bull/20 text-bull border-bull/40 shadow-sm'
                : 'border-border/80 bg-elevated/60 text-secondary hover:text-foreground hover:border-accent/40',
            )}
            title="【终极置顶】开启系统级永远置顶小窗（同花顺/通达信绝对无法遮挡，实时刷新并支持操作）"
          >
            <Tv className="h-3 w-3 text-bull" />
            <span>{pipWindow || isVideoPipActive ? '置顶中' : '系统置顶'}</span>
          </button>

          {/* 按钮 2: 手动添加标的 */}
          <button
            type="button"
            onClick={() => setShowAddForm(!showAddForm)}
            className={cn(
              'p-1 rounded transition-colors cursor-pointer',
              showAddForm
                ? 'bg-accent/20 text-accent'
                : 'hover:bg-elevated hover:text-foreground',
            )}
            title="手动添加股票代码加入盯盘"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>

          {/* 按钮 3: 独立小窗口 */}
          {!pipWindow && (
            <button
              type="button"
              onClick={openFloatingWindow}
              className="p-1 rounded hover:bg-elevated hover:text-foreground transition-colors cursor-pointer"
              title="弹出独立桌面小窗盯盘"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </button>
          )}

          {/* 按钮 4: 最小化/展开 */}
          {!pipWindow && (
            <button
              type="button"
              onClick={() => setMinimized(!minimized)}
              className="p-1 rounded hover:bg-elevated hover:text-foreground transition-colors cursor-pointer"
              title={minimized ? '展开' : '折叠'}
            >
              {minimized ? <Maximize2 className="h-3.5 w-3.5" /> : <Minimize2 className="h-3.5 w-3.5" />}
            </button>
          )}

          {/* 按钮 5: 关闭 */}
          <button
            type="button"
            onClick={() => {
              if (pipWindow) pipWindow.close()
              setPipOpen(false)
            }}
            className="p-1 rounded hover:bg-elevated hover:text-danger transition-colors cursor-pointer"
            title="关闭浮窗"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* 手动添加新标的展开面板 */}
      {showAddForm && (
        <div className="rounded-xl border border-border bg-base/70 p-2.5 flex flex-col gap-2 text-xs">
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              placeholder="代码 (如 600354 / 000878)"
              value={newSymbolInput}
              onChange={(e) => setNewSymbolInput(e.target.value)}
              className="flex-1 rounded border border-border bg-surface px-2 py-1 text-xs text-foreground focus:border-accent focus:outline-none"
              onKeyDown={(e) => {
                if (e.key === 'Enter') void handleAddNewSymbol()
              }}
            />
            <input
              type="number"
              step="0.01"
              placeholder="成本(选填)"
              value={newCostInput}
              onChange={(e) => setNewCostInput(e.target.value)}
              className="w-20 rounded border border-border bg-surface px-2 py-1 text-xs text-foreground focus:border-accent focus:outline-none"
              onKeyDown={(e) => {
                if (e.key === 'Enter') void handleAddNewSymbol()
              }}
            />
            <button
              type="button"
              disabled={addingLoading || !newSymbolInput.trim()}
              onClick={() => void handleAddNewSymbol()}
              className="rounded bg-accent px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1 cursor-pointer"
            >
              {addingLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : '加入'}
            </button>
          </div>
          {addError && <span className="text-[10px] text-danger">{addError}</span>}
        </div>
      )}

      {/* 标的卡片列表 */}
      {!minimized && (
        <div className="flex flex-col gap-2 max-h-[480px] overflow-y-auto pr-0.5">
          {positions.length === 0 ? (
            <div className="py-6 flex flex-col items-center justify-center text-center text-muted gap-2">
              <Target className="h-8 w-8 opacity-40 text-bull" />
              <div className="text-xs text-foreground font-medium">暂无动态盯盘标的</div>
              <p className="text-[10px] text-secondary max-w-[240px]">
                在选股列表中点击「我已买」或「盯盘」，或点击上方「+」直接输入股票代码开启实时移动止损监控。
              </p>
              <button
                type="button"
                onClick={() => setShowAddForm(true)}
                className="mt-1 inline-flex items-center gap-1 rounded-btn bg-bull/15 border border-bull/30 px-3 py-1 text-xs font-medium text-bull hover:bg-bull/25 transition-colors cursor-pointer"
              >
                <Plus className="h-3.5 w-3.5" />
                立即添加股票
              </button>
            </div>
          ) : (
            positions.map((item) => {
              const cur = item.currentPrice || item.costPrice
              const calc: StopLossCalculation = calculateDynamicStopLoss({
                costPrice: item.costPrice,
                currentPrice: cur,
                peakPrice: item.peakPrice,
                ma5: item.ma5,
                holdingDays: item.holdingDays,
              })

              const isTriggered = calc.state === 'TRIGGERED'
              const isWarning = calc.state === 'WARNING'
              const changePct = item.changePct ?? 0

              return (
                <div
                  key={item.symbol}
                  className={cn(
                    'relative rounded-xl border p-2.5 transition-all text-xs flex flex-col gap-2',
                    isTriggered
                      ? 'border-danger/80 bg-danger/10 shadow-lg shadow-danger/10 animate-pulse'
                      : isWarning
                        ? 'border-warning/80 bg-warning/10 shadow-md shadow-warning/5'
                        : 'border-border/70 bg-surface/80 hover:border-border',
                  )}
                >
                  {/* 标的与实时价格 */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-1.5">
                      <span className="font-semibold text-sm text-foreground">{item.name}</span>
                      <span className="font-mono text-[10px] text-muted">{item.symbol}</span>
                      {item.strategyName && (
                        <span className="rounded bg-elevated px-1 py-0.5 text-[9px] text-secondary">
                          {item.strategyName.replace('双刃合-', '')}
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          'font-mono font-bold text-sm',
                          changePct >= 0 ? 'text-bull' : 'text-bear',
                        )}
                      >
                        {fmtPrice(cur)}
                      </span>
                      <span
                        className={cn(
                          'font-mono text-[11px] font-medium flex items-center',
                          changePct >= 0 ? 'text-bull' : 'text-bear',
                        )}
                      >
                        {changePct >= 0 ? (
                          <TrendingUp className="h-3 w-3 mr-0.5 inline" />
                        ) : (
                          <TrendingDown className="h-3 w-3 mr-0.5 inline" />
                        )}
                        {fmtPct(changePct)}
                      </span>

                      <button
                        type="button"
                        onClick={() => removeMonitoredPosition(item.symbol)}
                        className="text-muted/50 hover:text-danger p-0.5 rounded transition-colors cursor-pointer"
                        title="移出监控"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </div>

                  {/* 成本价与冲高 Peak */}
                  <div className="grid grid-cols-2 gap-1.5 rounded-lg bg-base/60 p-1.5 text-[11px]">
                    <div className="flex items-center justify-between">
                      <span className="text-muted">买入成本</span>
                      {editingSymbol === item.symbol ? (
                        <div className="flex items-center gap-1">
                          <input
                            type="number"
                            step="0.01"
                            autoFocus
                            value={editingPrice}
                            onChange={(e) => setEditingPrice(e.target.value)}
                            onBlur={() => {
                              const p = parseFloat(editingPrice)
                              if (p > 0 && Math.abs(p - item.costPrice) > 0.001) {
                                addOrUpdateMonitoredPosition({
                                  ...item,
                                  costPrice: p,
                                  peakPrice: Math.max(p, item.currentPrice || 0),
                                })
                                toast(`已更新成本为 ${p.toFixed(2)} 并重置高点`, 'success')
                              }
                              setEditingSymbol(null)
                            }}
                            className="w-14 rounded border border-accent bg-surface px-1 py-0.5 text-right font-mono text-xs text-foreground focus:outline-none shadow-xs"
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') {
                                const p = parseFloat(editingPrice)
                                if (p > 0 && Math.abs(p - item.costPrice) > 0.001) {
                                  addOrUpdateMonitoredPosition({
                                    ...item,
                                    costPrice: p,
                                    peakPrice: Math.max(p, item.currentPrice || 0),
                                  })
                                  toast(`已更新成本为 ${p.toFixed(2)} 并重置高点`, 'success')
                                }
                                setEditingSymbol(null)
                              } else if (e.key === 'Escape') {
                                setEditingSymbol(null)
                              }
                            }}
                          />
                        </div>
                      ) : (
                        <span
                          onClick={() => {
                            setEditingSymbol(item.symbol)
                            setEditingPrice(String(item.costPrice))
                          }}
                          className="font-mono font-medium text-secondary hover:text-accent cursor-pointer flex items-center gap-0.5 group"
                          title="点击快速修改买入成本价 (修改后自动按新成本重置高点)"
                        >
                          <span className="underline decoration-dotted decoration-muted group-hover:decoration-accent">
                            {fmtPrice(item.costPrice)}
                          </span>
                          <Edit2 className="h-2.5 w-2.5 opacity-40 group-hover:opacity-100 text-accent" />
                        </span>
                      )}
                    </div>

                    <div className="flex items-center justify-between">
                      <span className="text-muted flex items-center gap-0.5">
                        最高 Peak
                        <ArrowUpRight className="h-2.5 w-2.5 text-bull" />
                      </span>
                      <div className="flex items-center gap-1">
                        <span className="font-mono font-medium text-bull">
                          {fmtPrice(calc.peakPrice)}
                        </span>
                        {calc.peakPrice > item.costPrice && (
                          <button
                            type="button"
                            onClick={() => {
                              addOrUpdateMonitoredPosition({
                                ...item,
                                peakPrice: Math.max(item.costPrice, item.currentPrice || 0),
                              })
                              toast(`已重置 ${item.name} 的 Peak 为当前买入基准`)
                            }}
                            className="p-0.5 rounded text-[9px] text-muted hover:text-foreground hover:bg-elevated cursor-pointer"
                            title="一键将 Peak 重新对齐为当前买入基准 (去除买入前的历史虚高点)"
                          >
                            <RotateCcw className="h-2.5 w-2.5" />
                          </button>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* 动态防守线与安全垫 */}
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between text-[11px]">
                      <span className="text-secondary flex items-center gap-1">
                        {isTriggered ? (
                          <ShieldAlert className="h-3 w-3 text-danger" />
                        ) : (
                          <ShieldCheck className="h-3 w-3 text-bull" />
                        )}
                        出场防守线:
                        <b className="font-mono text-foreground font-semibold">
                          {fmtPrice(calc.effectiveStopPrice)}
                        </b>
                        <span className="text-[9px] text-muted">
                          ({calc.effectiveExitReason === 'trailing_stop'
                            ? 'Peak-3.5%'
                            : calc.effectiveExitReason === 'stop_loss'
                              ? '-5%硬止损'
                              : calc.effectiveExitReason === 'ma5_breakdown'
                                ? '破MA5'
                                : '满3天'})
                        </span>
                      </span>

                      <span
                        className={cn(
                          'font-mono font-semibold px-1.5 py-0.5 rounded text-[10px]',
                          isTriggered
                            ? 'bg-danger/20 text-danger'
                            : isWarning
                              ? 'bg-warning/20 text-warning'
                              : 'bg-bull/15 text-bull',
                        )}
                      >
                        {isTriggered
                          ? '🚨 立即离场'
                          : `安全垫: +${calc.safetyMarginPct}%`}
                      </span>
                    </div>

                    {/* 安全垫可视化进度条 */}
                    <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-border/40">
                      <div
                        className={cn(
                          'h-full transition-all duration-300 rounded-full',
                          isTriggered
                            ? 'bg-danger w-full'
                            : isWarning
                              ? 'bg-warning'
                              : 'bg-bull',
                        )}
                        style={{
                          width: isTriggered
                            ? '100%'
                            : `${Math.max(5, Math.min(100, (calc.safetyMarginPct / 5) * 100))}%`,
                        }}
                      />
                    </div>

                    {/* 盈亏比率与状态 */}
                    <div className="flex items-center justify-between text-[10px] text-muted pt-0.5">
                      <span>
                        持仓盈亏:{' '}
                        <b
                          className={cn(
                            'font-mono',
                            calc.pnlPct >= 0 ? 'text-bull' : 'text-bear',
                          )}
                        >
                          {calc.pnlPct >= 0 ? `+${calc.pnlPct}%` : `${calc.pnlPct}%`}
                        </b>
                      </span>
                      <span>
                        {calc.trailingLabel}线:{' '}
                        <span className="font-mono text-secondary">{fmtPrice(calc.trailingStopPrice)}</span>
                        <span className="text-[9px] text-muted ml-0.5">
                          ({calc.isProfitLock ? '锁定利润' : '高点-3.5%'})
                        </span>
                      </span>
                    </div>
                  </div>
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )

  // 1. 若处于画中画窗口中，渲染至 pipWindow.document.body
  if (pipWindow) {
    return createPortal(content, pipWindow.document.body)
  }

  // 2. 否则渲染至页面内的悬浮卡片
  return (
    <div
      style={{
        transform: `translate3d(${pos.x}px, ${pos.y}px, 0)`,
      }}
      className="fixed top-0 left-0 z-[9990] w-[340px] rounded-2xl border border-border/80 bg-surface/95 backdrop-blur-xl shadow-2xl p-3 select-none transition-shadow hover:shadow-accent/10"
    >
      {content}
    </div>
  )
}
