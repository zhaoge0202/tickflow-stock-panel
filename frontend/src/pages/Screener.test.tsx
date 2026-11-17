// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Screener } from './Screener'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const state = vi.hoisted(() => ({
  pool: ['minute', 'daily'],
  prune: vi.fn(),
  prefs: { screener_auto_run: false },
  status: { latest_strategy_date: '2026-11-06', enriched: { latest_date: '2026-11-06', earliest_date: '2026-01-01' } },
}))
vi.mock('@/lib/api', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/api')>()
  return { ...original, api: { ...original.api,
    screenerStrategies: vi.fn(), screenerCachedSummary: vi.fn(), screenerCachedResult: vi.fn(),
    screenerRunPreset: vi.fn(), screenerRunAll: vi.fn(),
    screenerFocusVersions: vi.fn(), screenerPreselect: vi.fn(), screenerAuctionConfirmation: vi.fn(),
    strategyPurchaseMarks: vi.fn(async () => ({ marks: [] })),
    strategyHistory: vi.fn(async () => ({ events: [] })),
    strategyHistoryBackfill: vi.fn(async () => ({})),
    watchlistList: vi.fn(async () => ({ symbols: [] })),
    monitorRulesList: vi.fn(async () => ({ rules: [] })),
    minuteRefreshStatus: vi.fn(async () => ({ enabled: false, running: false })),
  } }
})
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: state.prefs }),
  useDataStatus: () => ({ data: state.status }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
  useQuoteStatus: () => ({ data: { running: false, is_trading_hours: false } }),
}))
vi.mock('@/lib/useStrategyPool', () => ({ useStrategyPool: () => ({
  pool: state.pool, prune: state.prune, addToPool: vi.fn(), removeFromPool: vi.fn(), reorderPool: vi.fn(),
}) }))
vi.mock('@/lib/useSharedMutations', () => ({ useWatchlistBatchAdd: () => ({ mutate: vi.fn(), isPending: false }) }))
vi.mock('@/lib/screener-columns', () => ({
  SCREENER_BUILTIN_COLUMNS: [], SCREENER_COLUMN_GROUPS: [],
  buildExtColumnsParam: () => '', loadScreenerColumnConfig: async () => [],
  saveScreenerColumnConfig: vi.fn(), refreshExtColumns: (columns: unknown) => columns,
}))
vi.mock('@/components/ListColumnCustomizer', () => ({ ListColumnCustomizer: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/DatePicker', () => ({ DatePicker: ({ onChange }: { onChange: (day: string) => void }) =>
  <button onClick={() => onChange('2026-11-05')}>查看前一交易日</button>,
}))
vi.mock('@/components/screener/ScreenerTable', () => ({
  ScreenerTable: ({ rows }: { rows: { symbol: string }[] }) => <output>{rows.map(row => row.symbol).join(',')}</output>,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const day = '2026-11-06'
const errorMessage = '策略参数已变化，请重新计算'

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  state.pool = ['minute', 'daily']
  state.prefs = { screener_auto_run: false }
  state.status = { latest_strategy_date: day, enriched: { latest_date: day, earliest_date: '2026-01-01' } }
  localStorage.clear()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request in Screener test') }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: [
    { id: 'minute', name: '分钟策略', source: 'custom', asset_types: ['stock'], timeframes: ['1m'] },
    { id: 'daily', name: '日线策略', source: 'custom', asset_types: ['stock'], timeframes: ['1d'] },
  ] } as any)
  vi.mocked(api.screenerCachedSummary).mockResolvedValue({ as_of: day, results: {
    minute: { as_of: day, total: 5 }, daily: { as_of: day, total: 1 },
  }, today_ever_counts: {}, minute_pending: [] } as any)
  vi.mocked(api.screenerCachedResult).mockResolvedValue({ result: {
    as_of: day, strategy: 'daily', total: 1, rows: [{ symbol: '000001.SZ', score: 1 }], elapsed_ms: 0,
  }, strategy_ids_by_symbol: {}, today_ever_rows: null } as any)
  vi.mocked(api.screenerRunPreset).mockRejectedValue(new Error(errorMessage))
  vi.mocked(api.screenerPreselect).mockResolvedValue({ mode: 'preselect', as_of: day, trade_date: day, updated_at: 0, results: {} })
  vi.mocked(api.screenerAuctionConfirmation).mockResolvedValue({ as_of: day, trade_date: day, gate_status: 'empty_candidates', updated_at: 0, results: {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function tick() {
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}

async function render() {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Screener /></QueryClientProvider></MemoryRouter>,
  ))
  await tick()
}

async function clickCard(name: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.includes(name))
  expect(button).toBeTruthy()
  await act(async () => button!.click())
  await tick()
}

it('clears a failed minute run when opening a counted daily card, without recalculating the cached strategy', async () => {
  await render()
  await clickCard('分钟策略')
  expect(host.textContent).toContain(errorMessage)

  await clickCard('日线策略')

  expect(api.screenerCachedResult).toHaveBeenCalledWith('daily', undefined)
  expect(host.textContent).toContain('000001.SZ')
  expect(host.textContent).not.toContain(errorMessage)
  expect(api.screenerRunPreset).toHaveBeenCalledTimes(1)
  expect(api.screenerRunAll).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})

it('still displays a genuine error from the currently selected strategy', async () => {
  await render()
  await clickCard('分钟策略')
  expect(host.textContent).toContain(errorMessage)
  expect(api.screenerRunPreset).toHaveBeenCalledWith('minute', undefined, undefined, undefined, 'stock', '1m', true)
})

it('does not attach a late failure from the previous card to the cached strategy now being viewed', async () => {
  let rejectRun!: (error: Error) => void
  vi.mocked(api.screenerRunPreset).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRun = reject }))
  await render()
  await clickCard('分钟策略')
  await clickCard('日线策略')

  await act(async () => rejectRun(new Error(errorMessage)))
  await tick()

  expect(host.textContent).toContain('000001.SZ')
  expect(host.textContent).not.toContain(errorMessage)
  expect(api.screenerRunPreset).toHaveBeenCalledTimes(1)
})

function focusPayload(asOf = day, pending = false, symbol = 'FOCUS_READY') {
  const detail = { label: '收盘正式版', time: '15:35', status: pending ? 'pending' : 'ready', total: pending ? 0 : 1, rows: pending ? [] : [{ symbol, close: 10 }] }
  return {
    as_of: asOf, strategy_id: 'custom_dual_edge_focus', strategy_name: '双刃合-Focus',
    current_stage: pending ? 'waiting_preview' : 'final', is_unclosed: false,
    versions: { final: detail, preview: detail, preselect: detail }, dropped_from_preview: [],
    summary: { preview_total: 0, final_total: pending ? 0 : 1, preselect_total: 0, confirmed_count: 0, dropped_count: 0, late_entrant_count: 0 },
    calculation: { status: pending ? 'pending' : 'ready', task_id: `focus-${asOf}` },
  } as any
}

function useFocus() {
  state.pool = ['custom_dual_edge_focus']
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: [
    { id: 'custom_dual_edge_focus', name: '双刃合-Focus', source: 'custom', asset_types: ['stock'], timeframes: ['1d'] },
  ] } as any)
  vi.mocked(api.screenerCachedSummary).mockResolvedValue({ as_of: day, results: {
    custom_dual_edge_focus: { as_of: day, total: 1 },
  }, today_ever_counts: {}, minute_pending: [] } as any)
  vi.mocked(api.screenerCachedResult).mockResolvedValue({ result: {
    as_of: day, strategy: 'custom_dual_edge_focus', total: 1, rows: [{ symbol: 'CACHED_FOCUS' }], elapsed_ms: 0,
  }, strategy_ids_by_symbol: {}, today_ever_rows: null } as any)
}

it('polls pending Focus versions without replacing cached rows with a no-match result', async () => {
  useFocus()
  vi.mocked(api.screenerFocusVersions)
    .mockResolvedValueOnce(focusPayload(day, true))
    .mockResolvedValue(focusPayload())
  await render()
  await clickCard('双刃合-Focus')

  expect(api.screenerFocusVersions).toHaveBeenCalledWith('custom_dual_edge_focus', day, 5, undefined, true)
  expect(host.textContent).toContain('正在计算')
  expect(host.textContent).toContain('CACHED_FOCUS')
  expect(host.textContent).not.toContain('今日无命中')

  await act(async () => { await vi.advanceTimersByTimeAsync(2300) })
  await tick()
  expect(host.textContent).toContain('FOCUS_READY')
  expect(host.textContent).not.toContain('正在计算')
  expect(api.screenerFocusVersions).toHaveBeenCalledTimes(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(4500) })
  expect(api.screenerFocusVersions).toHaveBeenCalledTimes(2)
})

it('does not display the previous date Focus results while the new date is pending', async () => {
  useFocus()
  vi.mocked(api.screenerFocusVersions).mockImplementation(async (_sid, asOf) => focusPayload(asOf, asOf !== day, 'PREVIOUS_DATE'))
  await render()
  await clickCard('双刃合-Focus')
  expect(host.textContent).toContain('PREVIOUS_DATE')

  const changeDate = [...host.querySelectorAll('button')].find(el => el.textContent === '查看前一交易日')!
  await act(async () => changeDate.click())
  await tick()

  expect(host.textContent).not.toContain('PREVIOUS_DATE')
  expect(host.textContent).not.toContain('CACHED_FOCUS')
  expect(host.textContent).toContain('正在计算')
  expect(host.textContent).not.toContain('今日无命中')
})

it('polls a pending preselect without replacing formal results with an empty pool', async () => {
  const sid = 'custom_dual_edge'
  state.pool = [sid]
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: [
    { id: sid, name: '双刃合', source: 'custom', asset_types: ['stock'], timeframes: ['1d'] },
  ] } as any)
  vi.mocked(api.screenerCachedSummary).mockResolvedValue({ as_of: day, results: {
    [sid]: { as_of: day, total: 1 },
  }, today_ever_counts: {}, minute_pending: [] } as any)
  vi.mocked(api.screenerCachedResult).mockResolvedValue({ result: {
    as_of: day, strategy: sid, total: 1, rows: [{ symbol: 'FORMAL_RESULT' }], elapsed_ms: 0,
  }, strategy_ids_by_symbol: {}, today_ever_rows: null } as any)
  vi.mocked(api.screenerPreselect).mockImplementation(async (_asOf, tradeDate) => ({
    mode: 'preselect', as_of: day, trade_date: tradeDate, updated_at: 0, results: {},
    calculation: { status: vi.mocked(api.screenerPreselect).mock.calls.length === 1 ? 'pending' : 'ready', task_id: 'preselect' },
  } as any))

  await render()
  await clickCard('双刃合')
  expect(api.screenerPreselect).toHaveBeenCalledWith(day, expect.any(String), [sid], 5, undefined, 'stock', true)
  expect(host.textContent).toContain('盘后预选正在计算')
  expect(host.textContent).toContain('FORMAL_RESULT')
  await act(async () => { await vi.advanceTimersByTimeAsync(2300) })
  await tick()
  expect(api.screenerPreselect).toHaveBeenCalledTimes(2)
  expect(host.textContent).not.toContain('盘后预选正在计算')
  expect(host.textContent).toContain('FORMAL_RESULT')
})

it('shows a background Focus failure and stops pending polling', async () => {
  useFocus()
  vi.mocked(api.screenerFocusVersions)
    .mockResolvedValueOnce(focusPayload(day, true))
    .mockRejectedValue(new Error('指标数据已变化，请重新计算'))
  await render()
  await clickCard('双刃合-Focus')
  await act(async () => { await vi.advanceTimersByTimeAsync(2300) })
  await tick()

  expect(host.textContent).toContain('结果计算失败：指标数据已变化，请重新计算')
  expect(host.textContent).not.toContain('正在计算')
  expect(host.textContent).toContain('CACHED_FOCUS')
  await act(async () => { await vi.advanceTimersByTimeAsync(4500) })
  expect(api.screenerFocusVersions).toHaveBeenCalledTimes(2)
})

it('does not retain a completed Focus snapshot when the backend starts a replacement task', async () => {
  useFocus()
  vi.mocked(api.screenerFocusVersions).mockResolvedValueOnce(focusPayload(day, false, 'OLD_DEFINITION'))
  await render()
  await clickCard('双刃合-Focus')
  expect(host.textContent).toContain('OLD_DEFINITION')

  vi.mocked(api.screenerFocusVersions).mockResolvedValue({
    ...focusPayload(day, true), calculation: { status: 'pending', task_id: 'new-definition' },
  })
  await act(async () => { await client.invalidateQueries({ queryKey: ['screenerFocusVersions'] }) })
  await tick()

  expect(host.textContent).not.toContain('OLD_DEFINITION')
  expect(host.textContent).toContain('CACHED_FOCUS')
  expect(host.textContent).toContain('正在计算')
})

it('backfills history only after an explicit click and does not repeat after completion', async () => {
  let finish!: () => void
  vi.mocked(api.strategyHistoryBackfill).mockImplementation(() => new Promise(resolve => { finish = () => resolve({} as any) }))
  await render()
  await clickCard('分钟策略')
  await clickCard('日线策略')
  expect(api.strategyHistoryBackfill).not.toHaveBeenCalled()

  const history = [...host.querySelectorAll('button')].find(el => el.textContent?.startsWith('推荐历史'))!
  await act(async () => history.click())
  await tick()
  const button = [...document.querySelectorAll('button')].find(el => el.textContent?.includes('补齐历史'))!
  expect(button).toBeTruthy()
  await act(async () => button.click())
  await tick()

  expect(api.strategyHistoryBackfill).toHaveBeenCalledWith(['daily'])
  const pending = [...document.querySelectorAll('button')].find(el => el.textContent?.includes('补齐中'))!
  expect(pending.disabled).toBe(true)
  await act(async () => finish())
  await tick()
  await act(async () => { await vi.advanceTimersByTimeAsync(4500) })
  expect(api.strategyHistoryBackfill).toHaveBeenCalledTimes(1)
})

it('publishes a history backfill to the originating strategy when the selection changes', async () => {
  let finish!: () => void
  vi.mocked(api.strategyHistoryBackfill).mockImplementation(() => new Promise(resolve => { finish = () => resolve({} as any) }))
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await render()
  await clickCard('日线策略')
  const history = [...host.querySelectorAll('button')].find(el => el.textContent?.startsWith('推荐历史'))!
  await act(async () => history.click())
  await tick()
  const button = [...document.querySelectorAll('button')].find(el => el.textContent?.includes('补齐历史'))!
  await act(async () => button.click())
  const close = document.querySelector<HTMLButtonElement>('[aria-label="关闭"]')!
  await act(async () => close.click())
  await clickCard('分钟策略')
  invalidate.mockClear()

  await act(async () => finish())
  await tick()

  expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.strategyHistory('daily', 180) })
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: QK.strategyHistory('minute', 180) })
})

it('reports an explicit history backfill failure', async () => {
  vi.mocked(api.strategyHistoryBackfill).mockRejectedValue(new Error('数据已变化'))
  await render()
  await clickCard('日线策略')
  const history = [...host.querySelectorAll('button')].find(el => el.textContent?.startsWith('推荐历史'))!
  await act(async () => history.click())
  await tick()
  const button = [...document.querySelectorAll('button')].find(el => el.textContent?.includes('补齐历史'))!
  await act(async () => button.click())
  await tick()

  expect(toast).toHaveBeenCalledWith('历史补齐失败：数据已变化', 'error')
})
