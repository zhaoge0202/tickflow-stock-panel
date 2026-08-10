import { useRef, useState } from 'react'
import { Check, FolderCog, FolderInput, Pencil, Plus, Trash2, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import type { WatchlistGroup, WatchlistGroupColor } from '@/lib/api'
import {
  DEFAULT_WATCHLIST_GROUP_COLOR,
  WATCHLIST_GROUP_COLORS,
  resolveWatchlistGroupColor,
} from '@/lib/watchlist-group-colors'

export type WatchlistGroupFilter = 'all' | 'ungrouped' | string

interface GroupBarProps {
  groups: WatchlistGroup[]
  counts: Record<string, number>
  selected: WatchlistGroupFilter
  total: number
  onSelect: (group: WatchlistGroupFilter) => void
  onCreate: (name: string, color: WatchlistGroupColor) => Promise<void>
  onRename: (groupId: string, name: string, color: WatchlistGroupColor) => Promise<void>
  onDelete: (groupId: string) => Promise<void>
}

export function WatchlistGroupBar({
  groups,
  counts,
  selected,
  total,
  onSelect,
  onCreate,
  onRename,
  onDelete,
}: GroupBarProps) {
  const [managerOpen, setManagerOpen] = useState(false)
  const tabs = [
    { id: 'all', name: '全部', count: total, color: null },
    { id: 'ungrouped', name: '未分组', count: counts.ungrouped ?? 0, color: null },
    ...groups.map(group => ({ id: group.id, name: group.name, count: counts[group.id] ?? 0, color: group.color })),
  ]

  return (
    <>
      <div className="flex h-10 items-stretch border-b border-border bg-surface/40 px-5">
        <div role="tablist" aria-label="自选分组" className="flex min-w-0 flex-1 items-stretch gap-1 overflow-x-auto">
          {tabs.map(tab => {
            const active = selected === tab.id
            const color = tab.color ? resolveWatchlistGroupColor(tab.color) : null
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => onSelect(tab.id)}
                className={`my-1.5 inline-flex shrink-0 items-center gap-1.5 rounded-btn border px-3 text-xs transition-colors ${
                  active
                    ? color
                      ? `${color.text} ${color.border} ${color.background}`
                      : 'border-accent/40 bg-accent/10 text-accent'
                    : color
                      ? `border-transparent ${color.text} hover:bg-elevated`
                      : 'border-transparent text-secondary hover:bg-elevated hover:text-foreground'
                }`}
              >
                {color && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${color.dot}`} />}
                <span>{tab.name}</span>
                <span className={`font-mono text-[10px] tabular-nums ${active && !color ? 'text-accent/80' : 'text-muted'}`}>
                  {tab.count}
                </span>
              </button>
            )
          })}
        </div>
        <button
          type="button"
          onClick={() => setManagerOpen(true)}
          className="ml-2 inline-flex w-8 shrink-0 items-center justify-center text-muted hover:text-accent"
          title="管理自选分组"
          aria-label="管理自选分组"
        >
          <FolderCog className="h-4 w-4" />
        </button>
      </div>

      {managerOpen && (
        <GroupManagerDialog
          groups={groups}
          counts={counts}
          onClose={() => setManagerOpen(false)}
          onCreate={onCreate}
          onRename={onRename}
          onDelete={onDelete}
        />
      )}
    </>
  )
}

function GroupColorPicker({
  value,
  onChange,
}: {
  value: WatchlistGroupColor
  onChange: (color: WatchlistGroupColor) => void
}) {
  return (
    <div role="radiogroup" aria-label="分组颜色" className="flex flex-wrap items-center gap-1.5">
      {WATCHLIST_GROUP_COLORS.map(option => {
        const selected = value === option.id
        return (
          <button
            key={option.id}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={option.label}
            title={option.label}
            onClick={() => onChange(option.id)}
            className={`inline-flex h-6 w-6 items-center justify-center rounded-full border transition-transform hover:scale-110 ${
              selected
                ? `${option.border} ${option.background} ring-2 ${option.ring}`
                : 'border-transparent hover:bg-elevated'
            }`}
          >
            <span className={`h-3 w-3 rounded-full ${option.dot}`} />
          </button>
        )
      })}
    </div>
  )
}

function GroupManagerDialog({
  groups,
  counts,
  onClose,
  onCreate,
  onRename,
  onDelete,
}: Omit<GroupBarProps, 'selected' | 'total' | 'onSelect'> & { onClose: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [newName, setNewName] = useState('')
  const [newColor, setNewColor] = useState<WatchlistGroupColor>(DEFAULT_WATCHLIST_GROUP_COLOR)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingName, setEditingName] = useState('')
  const [editingColor, setEditingColor] = useState<WatchlistGroupColor>(DEFAULT_WATCHLIST_GROUP_COLOR)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  const validate = (name: string) => {
    const value = name.trim()
    if (!value) return '请输入分组名称'
    if (value.length > 24) return '分组名称不能超过 24 个字符'
    return ''
  }

  const run = async (action: () => Promise<void>) => {
    setPending(true)
    setError('')
    try {
      await action()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setPending(false)
    }
  }

  const create = async () => {
    const message = validate(newName)
    if (message) {
      setError(message)
      return
    }
    await run(async () => {
      await onCreate(newName.trim(), newColor)
      setNewName('')
      setNewColor(DEFAULT_WATCHLIST_GROUP_COLOR)
      inputRef.current?.focus()
    })
  }

  const rename = async (groupId: string) => {
    const message = validate(editingName)
    if (message) {
      setError(message)
      return
    }
    await run(async () => {
      await onRename(groupId, editingName.trim(), editingColor)
      setEditingId(null)
    })
  }

  return (
    <Modal
      onClose={onClose}
      labelledBy="watchlist-groups-title"
      initialFocusRef={inputRef}
      panelClassName="w-[92vw] max-w-md bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div>
          <h2 id="watchlist-groups-title" className="text-sm font-semibold text-foreground">管理自选分组</h2>
          <p className="mt-0.5 text-[11px] text-muted">删除分组不会删除其中的股票</p>
        </div>
        <button type="button" onClick={onClose} className="h-8 w-8 inline-flex items-center justify-center text-muted hover:text-foreground" aria-label="关闭">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="px-4 py-3">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            value={newName}
            maxLength={24}
            onChange={event => setNewName(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') void create() }}
            placeholder="新分组名称"
            className="h-8 min-w-0 flex-1 rounded-btn border border-border bg-elevated px-3 text-xs text-foreground outline-none focus:border-accent/50"
          />
          <button
            type="button"
            onClick={() => void create()}
            disabled={pending}
            className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs text-white disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" />
            新建
          </button>
        </div>
        <div className="mt-2 flex items-start gap-2">
          <span className="mt-1 shrink-0 text-[11px] text-muted">分组颜色</span>
          <GroupColorPicker value={newColor} onChange={setNewColor} />
        </div>
        {error && <p className="mt-2 text-xs text-danger">{error}</p>}
      </div>

      <div className="max-h-[360px] overflow-y-auto border-t border-border px-4">
        {groups.length === 0 ? (
          <div className="py-10 text-center text-xs text-muted">暂无自定义分组</div>
        ) : groups.map(group => {
          const color = resolveWatchlistGroupColor(group.color)
          return (
          <div key={group.id} className="flex min-h-12 items-center gap-2 border-b border-border/60 last:border-0">
            <span className={`h-6 w-1 shrink-0 rounded-full ${color.dot}`} />
            {editingId === group.id ? (
              <div className="min-w-0 flex-1 py-2">
                <div className="flex items-center gap-1.5">
                  <input
                    value={editingName}
                    maxLength={24}
                    onChange={event => setEditingName(event.target.value)}
                    onKeyDown={event => { if (event.key === 'Enter') void rename(group.id) }}
                    className={`h-7 min-w-0 flex-1 rounded-btn border bg-elevated px-2 text-xs text-foreground outline-none ${resolveWatchlistGroupColor(editingColor).border}`}
                    autoFocus
                  />
                  <button type="button" disabled={pending} onClick={() => void rename(group.id)} className={`p-1 ${resolveWatchlistGroupColor(editingColor).text}`} title="保存分组">
                    <Check className="h-3.5 w-3.5" />
                  </button>
                  <button type="button" onClick={() => setEditingId(null)} className="p-1 text-muted hover:text-foreground" title="取消">
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                <div className="mt-1.5">
                  <GroupColorPicker value={editingColor} onChange={setEditingColor} />
                </div>
              </div>
            ) : deletingId === group.id ? (
              <>
                <span className="min-w-0 flex-1 text-xs text-secondary">
                  删除“{group.name}”？{(counts[group.id] ?? 0) > 0 ? ` ${counts[group.id]} 只股票将回到未分组。` : ''}
                </span>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => void run(async () => { await onDelete(group.id); setDeletingId(null) })}
                  className="rounded px-2 py-1 text-[11px] text-danger bg-danger/10 hover:bg-danger/20 disabled:opacity-50"
                >
                  确认
                </button>
                <button type="button" onClick={() => setDeletingId(null)} className="p-1 text-muted hover:text-foreground" aria-label="取消删除">
                  <X className="h-3.5 w-3.5" />
                </button>
              </>
            ) : (
              <>
                <span className={`min-w-0 flex-1 truncate text-xs ${color.text}`}>{group.name}</span>
                <span className="font-mono text-[10px] text-muted tabular-nums">{counts[group.id] ?? 0} 只</span>
                <button
                  type="button"
                  onClick={() => {
                    setEditingId(group.id)
                    setEditingName(group.name)
                    setEditingColor(group.color)
                    setDeletingId(null)
                    setError('')
                  }}
                  className="p-1 text-muted hover:text-accent"
                  title="编辑分组"
                >
                  <Pencil className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => { setDeletingId(group.id); setEditingId(null); setError('') }}
                  className="p-1 text-muted hover:text-danger"
                  title="删除分组"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </>
            )}
          </div>
          )
        })}
      </div>
    </Modal>
  )
}

interface GroupPickerProps {
  groups: WatchlistGroup[]
  groupId?: string | null
  symbol: string
  disabled?: boolean
  onChange: (symbol: string, groupId: string | null) => void
}

export function WatchlistGroupPicker({ groups, groupId, symbol, disabled, onChange }: GroupPickerProps) {
  const group = groups.find(item => item.id === groupId)
  const groupName = group?.name ?? '未分组'
  const color = resolveWatchlistGroupColor(group?.color)
  return (
    <label
      className={`relative inline-flex h-5 w-5 items-center justify-center rounded border transition-colors ${
        group
          ? `${color.text} ${color.border} ${color.background}`
          : 'border-transparent text-muted hover:border-accent/30 hover:text-accent'
      } ${disabled ? 'opacity-40' : ''}`}
      title={`分组：${groupName}`}
    >
      <FolderInput className="h-3.5 w-3.5" />
      <select
        value={groupId ?? ''}
        disabled={disabled}
        aria-label={`${symbol} 的分组`}
        onClick={event => event.stopPropagation()}
        onChange={event => onChange(symbol, event.target.value || null)}
        className="absolute inset-0 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
      >
        <option value="">未分组</option>
        {groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}
      </select>
    </label>
  )
}
