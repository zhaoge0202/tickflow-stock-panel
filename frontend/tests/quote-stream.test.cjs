const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const { QueryClient } = require('@tanstack/react-query')

// 运行真实 SSE hook 的事件处理器；只替换浏览器与 React 生命周期，查询失效使用真实 QueryClient。
function harness(enabled = true, pages) {
  const client = new QueryClient()
  let cleanup
  let reconnect
  const streams = []
  const keys = loadTs('queryKeys.ts', {})
  class EventSource {
    constructor() { this.listeners = {}; streams.push(this) }
    addEventListener(name, fn) { this.listeners[name] = fn }
    close() {}
  }
  const hook = loadTs('useQuoteStream.ts', {
    react: {
      useEffect: fn => { cleanup = fn() },
      useRef: value => ({ current: value }),
      useCallback: fn => fn,
      useSyncExternalStore: (_subscribe, get) => get(),
    },
    '@tanstack/react-query': { useQueryClient: () => client },
    './queryKeys': keys,
    './kline': {},
    './useQueryConfig': { getQueryConfig: () => ({ sse: { reconnectDelay: 5000 } }) },
    '@/components/Toast': { toast() {} },
    '@/components/AlertToast': { pushAlertToasts() {} },
    './reviewStore': { feedReviewEvent() {} },
  }, { EventSource, setTimeout: fn => { reconnect = fn }, clearTimeout() {} })
  hook.useQuoteStream(enabled, pages)
  return { client, hook, streams, reconnect: () => reconnect(), close: () => { cleanup(); client.clear() } }
}

function loadTs(file, dependencies, globals = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib', file), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, require: name => {
    assert.ok(name in dependencies, `未声明的依赖: ${name}`)
    return dependencies[name]
  }, ...globals }, { filename: file })
  return exports
}

function seed(client, keys) {
  for (const key of keys) client.setQueryData(key, { value: '断线前的数据' })
}

const refreshKeys = [['alerts', ''], ['alerts-total'], ['decision', 'queue'], ['alert-outcomes', 7]]

test('短于兜底轮询的断线重连会补拉告警、决策与后验收益', () => {
  const h = harness()
  h.streams[0].onopen()
  seed(h.client, refreshKeys)
  assert.equal(h.hook.useSmartPollingInterval(15000), 60000)
  h.streams[0].onerror()
  assert.equal(h.hook.useSmartPollingInterval(15000), 15000)
  h.reconnect()
  h.streams[1].onopen()
  for (const key of refreshKeys) assert.equal(h.client.getQueryState(key).isInvalidated, true, key.join('/'))
  assert.equal(h.hook.useSmartPollingInterval(15000), 60000)
  h.close()
})

test('连接补拉与行情事件都遵守页面刷新配置，告警补拉不受行情开关影响', () => {
  const h = harness(true, { watchlist: false, 'market-snapshot': false })
  const keys = [['watchlist-enriched'], ['market-snapshot', 'latest', 'latest'], ['quote-status'], ['market-breadth']]
  for (const trigger of [() => h.streams[0].onopen(), () => h.streams[0].listeners.quotes_updated()]) {
    seed(h.client, keys)
    trigger()
    assert.equal(h.client.getQueryState(keys[0]).isInvalidated, false)
    assert.equal(h.client.getQueryState(keys[1]).isInvalidated, false)
    assert.equal(h.client.getQueryState(keys[2]).isInvalidated, true)
    assert.equal(h.client.getQueryState(keys[3]).isInvalidated, true)
  }
  h.close()
  const disabled = harness(false)
  seed(disabled.client, [...refreshKeys, ['market-breadth']])
  disabled.streams[0].onopen()
  assert.equal(disabled.client.getQueryState(['market-breadth']).isInvalidated, false)
  for (const key of refreshKeys) assert.equal(disabled.client.getQueryState(key).isInvalidated, true)
  disabled.close()
})
