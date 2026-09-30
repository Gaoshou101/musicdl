/** Exercise log polling across a server restart with reused numeric IDs. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
const require = createRequire(import.meta.url)
const ts = require('typescript')
let cursor = 0, dirty = false, tree
const slots = [], effects = [], polling = [], requests = []
const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]))
const hooks = {
  useRef(value) { const i = cursor++; slots[i] ??= { current: value }; return slots[i] },
  useState(value) {
    const i = cursor++
    slots[i] ??= { value }
    return [slots[i].value, next => {
      slots[i].value = typeof next === 'function' ? next(slots[i].value) : next
      dirty = true
    }]
  },
  useCallback(fn, deps) {
    const i = cursor++
    if (!same(slots[i]?.deps, deps)) slots[i] = { fn, deps }
    return slots[i].fn
  },
  useEffect(fn, deps) {
    const i = cursor++
    if (!same(slots[i]?.deps, deps)) {
      slots[i]?.cleanup?.()
      slots[i] = { deps }
      effects.push(() => { slots[i].cleanup = fn() })
    }
  },
}
const entry = (id, message) => ({ id, message, time: '2026-09-30T00:00:00Z', level: 'info', logger: 'musicdl', traceback: null })
let page = { items: [entry(1, 'old process')], total: 1, last_id: 1, dropped: 0, generation: 'first', reset: false }
const api = {
  readEvents: async () => ({ items: [], total: 0 }),
  readAudit: async () => ({ items: [], total: 0 }),
  readServiceLogs: async (...args) => { requests.push(args); return page },
  errorMessage: String,
}
const exports = {}
const jsx = (type, props) => ({ type, props })
const code = ts.transpileModule(readFileSync(new URL('../src/app/dashboard/logs/page.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText
vm.runInNewContext(code, {
  exports,
  require(name) {
    if (name === 'react') return hooks
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
    if (name === '@phosphor-icons/react') return {}
    if (name === '@/lib/api') return api
    if (name === '@/lib/format') return {}
    if (name === '@/lib/usePolling') return {
      usePolling(callback, interval, enabled) {
        polling.push({ callback, interval, enabled })
        return () => callback(new AbortController().signal)
      },
    }
    throw new Error(name)
  },
})
function render() {
  for (let i = 0; i < 10; i++) {
    dirty = false; cursor = 0; polling.length = 0; tree = exports.default()
    for (const effect of effects.splice(0)) effect()
    if (!dirty) return
  }
  throw new Error('render loop')
}
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes)
  if (!node || typeof node !== 'object') return []
  return [node, ...nodes(node.props?.children)]
}
const visibleMessages = () => nodes(tree).filter(n => n.props?.entry?.message).map(n => n.props.entry.message)
async function poll() {
  await polling.find(p => p.enabled).callback(new AbortController().signal)
  render()
}
render()
await poll()
nodes(tree).find(n => n.type === 'button' && n.props.children?.[0] === '服务日志').props.onClick()
render()
await poll()
assert.deepEqual(visibleMessages(), ['old process'])
assert.equal(requests[0][4], undefined)
page = { ...page, items: [entry(1, 'new process')], generation: 'second', reset: true }
await poll()
assert.equal(requests[1][1], 1)
assert.equal(requests[1][4], 'first')
assert.deepEqual(visibleMessages(), ['new process'], 'restart replaces old IDs instead of deduplicating away new entries')
page = { ...page, items: [entry(2, 'new tail')], last_id: 2, total: 2, reset: false }
await poll()
assert.equal(requests[2][4], 'second')
assert.deepEqual(visibleMessages(), ['new process', 'new tail'])
page = { ...page, items: [], last_id: 0, total: 0, generation: 'third', reset: true }
await poll()
assert.deepEqual(visibleMessages(), [], 'an empty restarted buffer clears the old window')
console.log('Service log restart and cursor checks passed')
