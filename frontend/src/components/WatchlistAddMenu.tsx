import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, Folder, Inbox, List, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'

const MENU_WIDTH = 224
const MENU_MAX_HEIGHT = 320
const VIEWPORT_GAP = 8
const TRIGGER_GAP = 6

export interface WatchlistGroupMenuProps {
  children: ReactNode
  onSelect: (groupId: string | null) => void
  disabled?: boolean
  preferredGroupId?: string | null
  includeAll?: boolean
  counts?: Record<string, number>
  total?: number
  disableEmpty?: boolean
  menuLabel?: string
  align?: 'left' | 'right'
  triggerClassName?: string
  title?: string
  ariaLabel?: string
}

/**
 * 自选分组选择菜单。
 * 分组仅在菜单打开时读取，React Query 会在多个入口间共享同一份缓存。
 */
export function WatchlistGroupMenu({
  children,
  onSelect,
  disabled = false,
  preferredGroupId,
  includeAll = false,
  counts,
  total = 0,
  disableEmpty = false,
  menuLabel = '选择自选分组',
  align = 'right',
  triggerClassName = '',
  title = '加入自选',
  ariaLabel = title,
}: WatchlistGroupMenuProps) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0 })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuId = useId()

  const groupsQuery = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
    enabled: open,
    staleTime: 60_000,
  })
  const groups = groupsQuery.data?.groups ?? []
  const showPreferred = preferredGroupId !== undefined

  const placeMenu = () => {
    const trigger = triggerRef.current
    if (!trigger) return

    const rect = trigger.getBoundingClientRect()
    const menuHeight = Math.min(menuRef.current?.offsetHeight ?? MENU_MAX_HEIGHT, MENU_MAX_HEIGHT)
    const spaceBelow = window.innerHeight - rect.bottom
    const spaceAbove = rect.top
    const dropUp = spaceBelow < menuHeight + TRIGGER_GAP && spaceAbove > spaceBelow
    const top = dropUp
      ? Math.max(VIEWPORT_GAP, rect.top - menuHeight - TRIGGER_GAP)
      : Math.min(rect.bottom + TRIGGER_GAP, window.innerHeight - menuHeight - VIEWPORT_GAP)
    const rawLeft = align === 'left' ? rect.left : rect.right - MENU_WIDTH
    const left = Math.max(
      VIEWPORT_GAP,
      Math.min(rawLeft, window.innerWidth - MENU_WIDTH - VIEWPORT_GAP),
    )
    setPosition({ top, left })
  }

  const toggleMenu = () => {
    if (disabled) return
    if (open) {
      setOpen(false)
      return
    }
    placeMenu()
    setOpen(true)
  }

  useLayoutEffect(() => {
    if (!open) return
    placeMenu()
  }, [open, groups.length, groupsQuery.isPending])

  useEffect(() => {
    if (!open) return

    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as Node
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return
      setOpen(false)
    }
    // 菜单内部分组列表可滚动 (max-h-60), 其 scroll 事件不应触发关闭; 仅页面/祖先容器滚动时关闭
    const closeOnViewportChange = (event: Event) => {
      if (event.target instanceof Node && menuRef.current?.contains(event.target)) return
      setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
    }

    document.addEventListener('mousedown', closeOnOutsideClick)
    window.addEventListener('keydown', closeOnEscape, true)
    window.addEventListener('scroll', closeOnViewportChange, true)
    window.addEventListener('resize', closeOnViewportChange)
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick)
      window.removeEventListener('keydown', closeOnEscape, true)
      window.removeEventListener('scroll', closeOnViewportChange, true)
      window.removeEventListener('resize', closeOnViewportChange)
    }
  }, [open])

  useEffect(() => {
    if (!open || groupsQuery.isPending) return
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus()
  }, [open, groups.length, groupsQuery.isPending])

  const choose = (groupId: string | null) => {
    setOpen(false)
    onSelect(groupId)
  }

  const handleMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])
    if (items.length === 0) return

    event.preventDefault()
    const current = items.indexOf(document.activeElement as HTMLButtonElement)
    if (event.key === 'Home') items[0].focus()
    else if (event.key === 'End') items[items.length - 1].focus()
    else if (event.key === 'ArrowDown') items[(current + 1 + items.length) % items.length].focus()
    else items[(current - 1 + items.length) % items.length].focus()
  }

  const menuItemClass = 'flex h-8 w-full items-center gap-2 rounded-btn px-2 text-left text-xs text-secondary outline-none transition-colors hover:bg-elevated hover:text-foreground focus:bg-elevated focus:text-foreground disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-secondary'
  const showCounts = counts !== undefined
  const ungroupedCount = counts?.ungrouped ?? 0

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={event => { event.stopPropagation(); toggleMenu() }}
        disabled={disabled}
        className={triggerClassName}
        title={title}
        aria-label={ariaLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
      >
        {children}
      </button>

      {open && createPortal(
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={menuLabel}
          data-watchlist-group-menu
          onKeyDown={handleMenuKeyDown}
          style={{ position: 'fixed', top: position.top, left: position.left }}
          className="z-[10000] w-56 overflow-hidden rounded-card border border-border bg-surface p-1.5 shadow-[0_10px_32px_rgba(0,0,0,0.42)]"
        >
          <div className="px-2 pb-1.5 pt-1 text-[10px] font-medium text-muted">{menuLabel}</div>
          {groupsQuery.isPending ? (
            <div className="flex h-16 items-center justify-center text-muted">
              <LoaderCircle className="h-4 w-4 animate-spin" />
            </div>
          ) : groupsQuery.isError ? (
            <button
              type="button"
              onClick={() => void groupsQuery.refetch()}
              className="flex h-14 w-full items-center justify-center gap-1.5 rounded-btn text-xs text-danger hover:bg-danger/10"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              重新加载分组
            </button>
          ) : (
            <div className="max-h-60 overflow-y-auto">
              {includeAll && (
                <>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => choose('all')}
                    disabled={disableEmpty && total === 0}
                    className={menuItemClass}
                  >
                    <List className="h-3.5 w-3.5 shrink-0 text-accent" />
                    <span className="min-w-0 flex-1 truncate">全部自选</span>
                    {showCounts && <span className="font-mono text-[10px] tabular-nums text-muted">{total}</span>}
                  </button>
                  <div className="my-1 border-t border-border/70" />
                </>
              )}
              <button
                type="button"
                role="menuitem"
                onClick={() => choose(null)}
                disabled={disableEmpty && ungroupedCount === 0}
                className={menuItemClass}
              >
                <Inbox className="h-3.5 w-3.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate">未分组</span>
                {showCounts && <span className="font-mono text-[10px] tabular-nums text-muted">{ungroupedCount}</span>}
                {showPreferred && preferredGroupId == null && (
                  <Check className="h-3.5 w-3.5 shrink-0 text-accent" aria-label="当前分组" />
                )}
              </button>
              {!includeAll && groups.length > 0 && <div className="my-1 border-t border-border/70" />}
              {groups.map(group => {
                const color = resolveWatchlistGroupColor(group.color)
                return (
                  <button
                    key={group.id}
                    type="button"
                    role="menuitem"
                    onClick={() => choose(group.id)}
                    disabled={disableEmpty && (counts?.[group.id] ?? 0) === 0}
                    className={`${menuItemClass} border-l-2 ${color.border}`}
                    title={group.name}
                  >
                    <Folder className={`h-3.5 w-3.5 shrink-0 ${color.text}`} />
                    <span className="min-w-0 flex-1 truncate">{group.name}</span>
                    {showCounts && <span className="font-mono text-[10px] tabular-nums text-muted">{counts[group.id] ?? 0}</span>}
                    {showPreferred && preferredGroupId === group.id && (
                      <Check className={`h-3.5 w-3.5 shrink-0 ${color.text}`} aria-label="当前分组" />
                    )}
                  </button>
                )
              })}
            </div>
          )}
        </div>,
        document.body,
      )}
    </>
  )
}

type WatchlistAddMenuProps = Omit<
  WatchlistGroupMenuProps,
  'includeAll' | 'counts' | 'total' | 'disableEmpty' | 'menuLabel'
>

/** 所有“加入自选”入口共用的目标分组菜单。 */
export function WatchlistAddMenu(props: WatchlistAddMenuProps) {
  return <WatchlistGroupMenu {...props} menuLabel="加入到自选分组" />
}
