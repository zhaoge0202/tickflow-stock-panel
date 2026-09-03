import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { FileText, ImagePlus, Keyboard, Loader2, Plus, Upload, X } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type WatchlistGroup, type WatchlistGroupColor, type WatchlistImportCandidate, type WatchlistImportResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import {
  DEFAULT_WATCHLIST_GROUP_COLOR,
  WATCHLIST_GROUP_COLORS,
  resolveWatchlistGroupColor,
} from '@/lib/watchlist-group-colors'

interface Props {
  open: boolean
  onClose: () => void
  /** 页面当前所在分组，作为默认目标分组（null=未分组）。 */
  groupId?: string | null
  /** 分组列表与成员快照由页面持有并下发，避免弹窗重复拉取。 */
  groups: WatchlistGroup[]
  existingBySymbol: ReadonlyMap<string, string[]>
}

const MAX_IMPORT_IMAGES = 10
const NO_MATCH_MSG = '未能匹配证券主数据，已跳过'
const DROP_ACCEPT =
  'image/jpeg,image/png,image/webp,image/bmp,image/gif,text/csv,text/plain,' +
  '.csv,.txt,.jpg,.jpeg,.png,.webp,.bmp,.gif'

interface RowState {
  eligible: boolean
  inWatchlist: boolean
  inAllSelected: boolean
}

/**
 * 未分组目标（空数组）只收新增；选定分组时，同时属于全部所选分组的标的不再可加。
 * 其余已匹配标的可勾选（并入尚未属于的分组）。
 */
function rowState(
  sym: string | null,
  matched: boolean,
  membership: ReadonlyMap<string, string[]>,
  targetIds: string[],
): RowState {
  if (!matched || !sym) return { eligible: false, inWatchlist: false, inAllSelected: false }
  const gids = membership.get(sym)
  const inWatchlist = gids !== undefined
  const inAllSelected = targetIds.length > 0
    && gids !== undefined && targetIds.every(gid => gids.includes(gid))
  const eligible = targetIds.length === 0 ? !inWatchlist : !inAllSelected
  return { eligible, inWatchlist, inAllSelected }
}

function isImageFile(file: File): boolean {
  return file.type.startsWith('image/') || /\.(jpe?g|png|webp|bmp|gif)$/i.test(file.name)
}

function isCsvFile(file: File): boolean {
  return /\.(csv|txt)$/i.test(file.name)
}

/** 多来源候选（多图 / 截图+文件混搭）按 code 合并，保留已匹配项。 */
export function mergeImportCandidates(
  lists: WatchlistImportCandidate[][],
): WatchlistImportCandidate[] {
  const byCode = new Map<string, WatchlistImportCandidate>()
  for (const list of lists) {
    for (const c of list) {
      const prev = byCode.get(c.code)
      if (!prev) {
        byCode.set(c.code, c)
        continue
      }
      if (c.matched && !prev.matched) {
        byCode.set(c.code, c)
        continue
      }
      if (c.matched && prev.matched) {
        byCode.set(c.code, {
          ...prev,
          symbol: prev.symbol || c.symbol,
          name: prev.name || c.name,
          already_in_watchlist: prev.already_in_watchlist || c.already_in_watchlist,
        })
      }
    }
  }
  return [...byCode.values()]
}

type Row = { c: WatchlistImportCandidate; state: RowState }

function Dropzone({
  busy,
  label,
  onPick,
}: {
  busy: boolean
  label: string
  onPick: (list: FileList | File[] | null | undefined) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={DROP_ACCEPT}
        className="hidden"
        onChange={e => {
          onPick(e.target.files)
          e.target.value = ''
        }}
      />
      <button
        type="button"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        onDragOver={e => { e.preventDefault(); e.stopPropagation() }}
        onDrop={e => {
          e.preventDefault()
          onPick(e.dataTransfer.files)
        }}
        className="w-full flex flex-col items-center justify-center gap-2 rounded-btn border border-dashed border-border bg-elevated/40 hover:bg-elevated/70 px-4 py-6 text-secondary transition-colors disabled:opacity-50"
      >
        {busy ? (
          <Loader2 className="h-6 w-6 animate-spin text-accent" />
        ) : (
          <ImagePlus className="h-6 w-6 text-accent" />
        )}
        <span className="text-xs">{busy ? label : '点击选择或拖拽券商自选截图 / CSV / TXT'}</span>
      </button>
    </>
  )
}

export function WatchlistImportDialog({
  open,
  onClose,
  groupId,
  groups,
  existingBySymbol,
}: Props) {
  const abortRef = useRef<AbortController | null>(null)
  const genRef = useRef(0)
  const qc = useQueryClient()

  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [candidates, setCandidates] = useState<WatchlistImportCandidate[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [previewUrls, setPreviewUrls] = useState<string[]>([])
  const [sourceFile, setSourceFile] = useState('')
  const [pasteOpen, setPasteOpen] = useState(false)
  const [codesText, setCodesText] = useState('')
  const [showSkipped, setShowSkipped] = useState(false)
  const [targetGroupIds, setTargetGroupIds] = useState<string[]>([])
  const [newGroupOpen, setNewGroupOpen] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [newGroupColor, setNewGroupColor] = useState<WatchlistGroupColor>(DEFAULT_WATCHLIST_GROUP_COLOR)
  const [creatingGroup, setCreatingGroup] = useState(false)
  const batchAdd = useWatchlistBatchAdd()

  const membership = existingBySymbol
  const groupNameById = useMemo(() => {
    const m = new Map<string, string>()
    for (const g of groups) m.set(g.id, g.name)
    return m
  }, [groups])

  const { eligible, skipped } = useMemo(() => {
    const eligible: Row[] = []
    const skipped: Row[] = []
    for (const c of candidates) {
      const state = rowState(c.symbol, c.matched, membership, targetGroupIds)
      ;(state.eligible ? eligible : skipped).push({ c, state })
    }
    return { eligible, skipped }
  }, [candidates, membership, targetGroupIds])
  const skippedCount = skipped.length
  const matchedCount = useMemo(
    () => candidates.filter(c => c.matched && c.symbol).length,
    [candidates],
  )

  const abortInFlight = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    genRef.current += 1
  }, [])

  const revokePreviews = useCallback((urls: string[]) => {
    for (const url of urls) URL.revokeObjectURL(url)
  }, [])

  const reset = useCallback(() => {
    abortInFlight()
    setBusy(false)
    setProgress(null)
    setCandidates([])
    setSelected(new Set())
    setPreviewUrls(prev => {
      revokePreviews(prev)
      return []
    })
    setSourceFile('')
    setPasteOpen(false)
    setCodesText('')
    setShowSkipped(false)
    setNewGroupOpen(false)
    setNewGroupName('')
    setNewGroupColor(DEFAULT_WATCHLIST_GROUP_COLOR)
  }, [abortInFlight, revokePreviews])

  useEffect(() => {
    if (!open) {
      reset()
      return
    }
    setTargetGroupIds(groupId ? [groupId] : [])
  }, [open, groupId, reset])

  const defaultSelection = (list: WatchlistImportCandidate[]) => {
    const out = new Set<string>()
    for (const c of list) {
      if (c.matched && c.symbol && !membership.has(c.symbol)) out.add(c.symbol)
    }
    return out
  }

  /** 切目标分组后，只摘掉不再可选的已勾标的（新增默认勾选在解析时一次性设定）。 */
  const changeTargetGroups = (next: string[]) => {
    setTargetGroupIds(next)
    const eligibleNow = new Set<string>()
    for (const c of candidates) {
      const sym = c.symbol
      if (sym && rowState(sym, c.matched, membership, next).eligible) eligibleNow.add(sym)
    }
    setSelected(prev => {
      let changed = false
      const kept = new Set<string>()
      for (const sym of prev) {
        if (eligibleNow.has(sym)) kept.add(sym)
        else changed = true
      }
      return changed ? kept : prev
    })
  }

  const stage = async (run: (signal: AbortSignal) => Promise<WatchlistImportResult>) => {
    abortInFlight()
    const controller = new AbortController()
    abortRef.current = controller
    const gen = genRef.current
    setBusy(true)
    try {
      const res = await run(controller.signal)
      if (gen !== genRef.current) return
      setCandidates(res.candidates)
      setSelected(defaultSelection(res.candidates))
      if (res.candidates.length > 0 && res.matched_count === 0) toast(NO_MATCH_MSG, 'error')
    } catch {
      /* 请求错误已由 request 封装弹出 */
    } finally {
      if (gen === genRef.current) setBusy(false)
    }
  }

  const runRecognizeQueue = async (files: File[]) => {
    const images = files.filter(isImageFile)
    const queue = images.slice(0, MAX_IMPORT_IMAGES)
    if (images.length > MAX_IMPORT_IMAGES) {
      toast(`一次最多识别 ${MAX_IMPORT_IMAGES} 张，已取前 ${MAX_IMPORT_IMAGES} 张`, 'error')
    }
    setPreviewUrls(prev => {
      revokePreviews(prev)
      return queue.map(f => URL.createObjectURL(f))
    })
    setSourceFile('')
    setShowSkipped(false)
    const controller = new AbortController()
    abortRef.current = controller
    const gen = genRef.current
    const merged: WatchlistImportCandidate[][] = []
    let failed = 0
    let lastError = ''
    setProgress({ done: 0, total: queue.length })
    setBusy(true)
    try {
      for (let i = 0; i < queue.length; i++) {
        try {
          const res = await api.watchlistImportImage(queue[i], controller.signal, true)
          merged.push(res.candidates)
        } catch (err) {
          if (gen !== genRef.current || controller.signal.aborted) return
          failed += 1
          lastError = err instanceof Error ? err.message : ''
        }
        setProgress({ done: i + 1, total: queue.length })
      }
      if (gen !== genRef.current) return
      const all = mergeImportCandidates(merged)
      setCandidates(all)
      setSelected(defaultSelection(all))
      if (all.length === 0) {
        toast(
          lastError
            || (failed > 0 ? '识别失败或未识别到股票代码' : '未识别到股票代码，请换更清晰的截图'),
          'error',
        )
      } else if (all.every(c => !c.matched)) {
        toast(NO_MATCH_MSG, 'error')
      } else if (failed > 0) {
        toast(`有 ${failed} 张识别失败，已合并其余结果`, 'error')
      }
    } finally {
      if (gen === genRef.current) {
        setBusy(false)
        setProgress(null)
      }
    }
  }

  const runCsvImport = async (file: File) => {
    setSourceFile(file.name)
    setPreviewUrls(prev => { revokePreviews(prev); return [] })
    setShowSkipped(false)
    await stage(signal => api.watchlistImportCsv(file, signal))
  }

  const runCodesParse = async () => {
    setSourceFile('')
    setShowSkipped(false)
    await stage(signal => api.watchlistImportCodes(codesText.trim(), signal))
  }

  const onSourcePick = async (list: FileList | File[] | null | undefined) => {
    if (!list || list.length === 0) return
    const files = Array.from(list)
    const images = files.filter(isImageFile)
    const csvs = files.filter(isCsvFile)
    if (csvs.length > 0) {
      if (csvs.length > 1 || images.length > 0) {
        toast('截图与 CSV 请分别导入', 'error')
        return
      }
      await runCsvImport(csvs[0])
      return
    }
    if (images.length > 0) {
      if (images.length < files.length) toast('已忽略非截图文件', 'error')
      await runRecognizeQueue(images)
      return
    }
    toast('请选择券商自选截图或 CSV / TXT 文件', 'error')
  }

  const runCodes = async () => {
    if (!codesText.trim()) {
      toast('请粘贴或输入股票代码', 'error')
      return
    }
    await runCodesParse()
  }

  const createGroup = async () => {
    const name = newGroupName.trim()
    if (!name) return
    setCreatingGroup(true)
    try {
      const data = await api.watchlistGroupCreate(name, newGroupColor)
      // 服务端返回全量分组列表；侧栏/分组条与本弹窗共用 QK.watchlistGroups 缓存
      qc.setQueryData(QK.watchlistGroups, { groups: data.groups })
      changeTargetGroups(
        targetGroupIds.includes(data.group.id)
          ? targetGroupIds
          : [...targetGroupIds, data.group.id],
      )
      setNewGroupOpen(false)
      setNewGroupName('')
      setNewGroupColor(DEFAULT_WATCHLIST_GROUP_COLOR)
    } catch {
      /* 已由 request 弹出 */
    } finally {
      setCreatingGroup(false)
    }
  }

  const toggle = (symbol: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(symbol)) next.delete(symbol)
      else next.add(symbol)
      return next
    })
  }

  let allSelected = eligible.length > 0
  for (const row of eligible) {
    if (!selected.has(row.c.symbol!)) {
      allSelected = false
      break
    }
  }

  const toggleAll = () => {
    setSelected(prev => {
      const next = new Set(prev)
      for (const row of eligible) {
        if (allSelected) next.delete(row.c.symbol!)
        else next.add(row.c.symbol!)
      }
      return next
    })
  }

  const confirmAdd = async () => {
    const symbols = [...selected]
    if (symbols.length === 0) {
      toast('请至少选择一只股票', 'error')
      return
    }
    const newCount = symbols.filter(sym => !membership.has(sym)).length
    const mergedCount = symbols.length - newCount
    try {
      await batchAdd.mutateAsync({ symbols, groupIds: targetGroupIds })
      const names = targetGroupIds
        .map(id => groupNameById.get(id))
        .filter((n): n is string => !!n)
        .join('、')
      if (names) {
        toast(
          mergedCount > 0
            ? `已导入 ${symbols.length} 只到「${names}」（新增 ${newCount}，并入 ${mergedCount}）`
            : `已导入 ${newCount} 只到「${names}」`,
          'success',
        )
      } else {
        toast(`已添加 ${newCount} 只自选`, 'success')
      }
      onClose()
    } catch {
      /* 已由 request 弹出 */
    }
  }

  if (!open) return null

  const progressLabel =
    progress && progress.total > 1
      ? `识别中 ${progress.done}/${progress.total}…`
      : progress
        ? '识别中…'
        : null

  const renderRow = ({ c, state }: Row) => {
    const sym = c.symbol
    const checked = !!sym && selected.has(sym)
    let status: ReactNode = null
    if (!c.matched || !sym) {
      status = <span className="text-[10px] text-warning/90">{NO_MATCH_MSG}</span>
    } else if (state.inAllSelected) {
      status = <span className="text-[10px] text-muted">已在所选分组</span>
    } else if (state.inWatchlist) {
      status = (
        <span className="text-[10px] text-muted">
          {targetGroupIds.length > 0 ? '已在自选 · 将并入所选分组' : '已在自选'}
        </span>
      )
    }
    return (
      <li key={sym ?? c.code}>
        <label
          className={`flex items-center gap-3 px-3 py-2.5 text-sm ${
            state.eligible ? 'cursor-pointer hover:bg-elevated/50' : 'opacity-50 cursor-not-allowed'
          }`}
        >
          <input
            type="checkbox"
            disabled={!state.eligible}
            checked={checked}
            onChange={() => sym && toggle(sym)}
            className="rounded border-border"
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-2">
              <span className="font-medium text-foreground truncate">
                {c.name || (c.matched && sym ? sym : '未匹配')}
              </span>
              <span className="text-[11px] text-muted tabular-nums shrink-0">
                {c.code}
                {sym ? ` · ${sym}` : ''}
              </span>
            </div>
            {status}
          </div>
        </label>
      </li>
    )
  }

  return (
    <Modal
      onClose={onClose}
      labelledBy="watchlist-import-title"
      panelClassName="w-[92vw] max-w-lg max-h-[85vh] flex flex-col bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <div>
          <h2 id="watchlist-import-title" className="text-sm font-semibold text-foreground">
            批量导入自选
          </h2>
          <p className="text-[11px] text-muted mt-0.5">
            截图 / CSV / TXT / 粘贴代码均支持，解析后按证券主数据匹配，可多选分组导入
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
          aria-label="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="px-4 py-3 overflow-y-auto flex-1 space-y-3">
        {busy && progressLabel && (
          <p className="text-[11px] text-muted">{progressLabel}</p>
        )}

        <div className="space-y-2">
          <Dropzone busy={busy} label={progressLabel ?? '解析中…'} onPick={(l) => void onSourcePick(l)} />
          {!pasteOpen ? (
            <button
              type="button"
              onClick={() => setPasteOpen(true)}
              className="w-full inline-flex items-center justify-center gap-1.5 rounded-btn border border-dashed border-border bg-elevated/40 px-3 py-2 text-xs text-secondary hover:bg-elevated/70"
            >
              <Keyboard className="h-3.5 w-3.5 text-accent" />
              或粘贴证券代码
            </button>
          ) : (
            <div className="space-y-2 rounded-btn border border-border bg-elevated/40 p-2.5">
              <textarea
                autoFocus
                value={codesText}
                onChange={e => setCodesText(e.target.value)}
                onKeyDown={e => {
                  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') void runCodes()
                }}
                placeholder={'示例：\n600519\n000001 平安银行\n515880 通信ETF国泰'}
                rows={4}
                className="w-full resize-y rounded-btn border border-border bg-surface px-3 py-2 text-xs text-foreground placeholder:text-muted focus:border-accent/50 focus:outline-none"
              />
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-muted">空格 / 逗号 / 换行分隔，Ctrl/⌘+Enter 解析</span>
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => { setPasteOpen(false); setCodesText('') }}
                    className="h-7 px-2 rounded-btn text-[11px] text-secondary hover:bg-elevated"
                  >
                    收起
                  </button>
                  <button
                    type="button"
                    disabled={busy || !codesText.trim()}
                    onClick={() => void runCodes()}
                    className="h-7 px-3 rounded-btn text-xs inline-flex items-center gap-1.5 bg-accent text-white hover:bg-accent/90 disabled:opacity-40"
                  >
                    {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Keyboard className="h-3.5 w-3.5" />}
                    解析
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>

        {previewUrls.length > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-1">
            {previewUrls.map((url, i) => (
              <div
                key={url}
                className="shrink-0 w-16 h-16 rounded-btn overflow-hidden border border-border bg-black/40"
              >
                <img src={url} alt={`预览 ${i + 1}`} className="w-full h-full object-contain" />
              </div>
            ))}
          </div>
        )}

        {sourceFile && (
          <div className="flex items-center gap-2 rounded-btn border border-border bg-elevated/40 px-3 py-2 text-xs text-secondary">
            <FileText className="h-3.5 w-3.5 shrink-0 text-accent" />
            <span className="truncate flex-1">{sourceFile}</span>
          </div>
        )}

        {candidates.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-secondary">
                {candidates.length} 个代码 · 匹配 {matchedCount} · 可添加 {eligible.length}
              </span>
              {eligible.length > 0 ? (
                <button
                  type="button"
                  onClick={toggleAll}
                  className="text-[11px] text-accent hover:underline shrink-0"
                >
                  {allSelected ? '取消全选' : '全选可添加'}
                </button>
              ) : matchedCount > 0 ? (
                <span className="text-[11px] text-muted shrink-0">所选目标分组已包含这些标的，无需导入</span>
              ) : null}
            </div>

            {skippedCount > 0 && (
              <button
                type="button"
                onClick={() => setShowSkipped(v => !v)}
                className="flex items-center gap-1 text-[11px] text-muted hover:text-secondary"
              >
                <span className="transition-transform" style={{ transform: showSkipped ? 'rotate(90deg)' : undefined }}>▸</span>
                已略过 {skippedCount} 个（已在自选/所选分组，或主数据未匹配）
              </button>
            )}

            <ul className="divide-y divide-border/60 rounded-btn border border-border overflow-hidden">
              {eligible.map(renderRow)}
              {showSkipped && skipped.map(renderRow)}
            </ul>
          </div>
        )}
      </div>

      <div className="px-4 py-3 border-t border-border shrink-0 space-y-2.5">
        <div className="space-y-1.5">
          <div className="flex items-start gap-2">
            <span className="text-[11px] text-secondary pt-1.5 shrink-0">导入到分组</span>
            <div className="flex flex-wrap items-center gap-1.5 min-w-0">
              <button
                type="button"
                onClick={() => changeTargetGroups([])}
                aria-pressed={targetGroupIds.length === 0}
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] transition-colors ${
                  targetGroupIds.length === 0
                    ? 'border-accent/40 bg-accent/10 text-accent'
                    : 'border-border bg-elevated text-secondary hover:text-foreground'
                }`}
                title="不加到任何分组，仅新增标的到自选"
              >
                未分组
              </button>
              <span className="mx-1 h-3 w-px shrink-0 self-center bg-border/60" aria-hidden="true" />
              {groups.map(g => {
                const active = targetGroupIds.includes(g.id)
                const c = resolveWatchlistGroupColor(g.color)
                return (
                  <button
                    key={g.id}
                    type="button"
                    onClick={() => changeTargetGroups(
                      active
                        ? targetGroupIds.filter(id => id !== g.id)
                        : [...targetGroupIds, g.id],
                    )}
                    aria-pressed={active}
                    className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] transition-colors ${
                      active
                        ? `${c.border} ${c.background} ${c.text}`
                        : 'border-border bg-elevated text-secondary hover:bg-elevated/80 hover:text-foreground'
                    }`}
                    title={`${active ? '移出' : '加入'}目标分组「${g.name}」`}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${active ? c.dot : 'bg-border'}`} />
                    {g.name}
                  </button>
                )
              })}
              {!newGroupOpen && (
                <button
                  type="button"
                  onClick={() => setNewGroupOpen(true)}
                  className="inline-flex items-center gap-1 rounded-full border border-dashed border-border bg-elevated/40 px-2 py-1 text-[11px] text-accent hover:bg-elevated/70"
                  title="新建分组接收这批导入"
                >
                  <Plus className="h-3 w-3" />
                  新建
                </button>
              )}
            </div>
          </div>
          {newGroupOpen && (
            <div className="rounded-btn border border-border bg-elevated/40 p-2 space-y-2">
              <div className="flex items-center gap-1.5">
                <input
                  autoFocus
                  maxLength={24}
                  value={newGroupName}
                  onChange={e => setNewGroupName(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') void createGroup()
                    if (e.key === 'Escape') { setNewGroupOpen(false); setNewGroupName(''); setNewGroupColor(DEFAULT_WATCHLIST_GROUP_COLOR) }
                  }}
                  placeholder="新分组名称，Enter 创建"
                  className="h-8 min-w-0 flex-1 rounded-btn border border-border bg-surface px-2 text-xs text-foreground placeholder:text-muted focus:border-accent/50 focus:outline-none"
                  aria-label="新分组名称"
                />
                <button
                  type="button"
                  onClick={() => void createGroup()}
                  disabled={creatingGroup || !newGroupName.trim()}
                  title="创建分组，并作为本次导入的目标分组"
                  className="h-8 shrink-0 px-3 rounded-btn text-xs inline-flex items-center gap-1.5 bg-accent text-white hover:bg-accent/90 disabled:opacity-40"
                >
                  {creatingGroup ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                  创建
                </button>
              </div>
              <div className="flex items-center gap-2 pl-1">
                <span className="text-[11px] text-secondary shrink-0">颜色</span>
                <div className="flex flex-wrap items-center gap-1.5">
                  {WATCHLIST_GROUP_COLORS.map(option => {
                    const active = option.id === newGroupColor
                    return (
                      <button
                        key={option.id}
                        type="button"
                        onClick={() => setNewGroupColor(option.id)}
                        aria-pressed={active}
                        aria-label={`颜色 ${option.label}`}
                        title={option.label}
                        className={`h-4 w-4 rounded-full transition-transform ${option.dot} ${active ? `ring-2 ${option.ring} scale-110` : 'hover:scale-110'}`}
                      />
                    )
                  })}
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="h-8 px-3 rounded-btn text-xs text-secondary hover:bg-elevated"
          >
            取消
          </button>
          <button
            type="button"
            disabled={selected.size === 0 || batchAdd.isPending || busy}
            onClick={() => void confirmAdd()}
            className="h-8 px-3 rounded-btn text-xs inline-flex items-center gap-1.5 bg-accent text-white hover:bg-accent/90 disabled:opacity-40"
          >
            {batchAdd.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Upload className="h-3.5 w-3.5" />
            )}
            导入所选 ({selected.size})
          </button>
        </div>
      </div>
    </Modal>
  )
}
