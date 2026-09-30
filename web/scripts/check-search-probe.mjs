/** Exercise the real search component with controlled hooks and deferred probes. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
const require = createRequire(import.meta.url)
const ts = require('typescript')
let cursor = 0, dirty = false, tree
const slots = [], effects = [], calls = []
const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]))
const hooks = {
  useRef(initial) {
    const i = cursor++
    slots[i] ??= { current: initial }
    return slots[i]
  },
  useState(initial) {
    const i = cursor++
    slots[i] ??= { value: initial }
    return [slots[i].value, (next) => { slots[i].value = typeof next === 'function' ? next(slots[i].value) : next; dirty = true }]
  },
  useMemo(fn, deps) {
    const i = cursor++
    if (!same(slots[i]?.deps, deps)) slots[i] = { value: fn(), deps }
    return slots[i].value
  },
  useCallback(fn, deps) { return hooks.useMemo(() => fn, deps) },
  useEffect(fn, deps) {
    const i = cursor++
    if (!same(slots[i]?.deps, deps)) {
      slots[i]?.cleanup?.()
      slots[i] = { deps }
      effects.push(() => { slots[i].cleanup = fn() })
    }
  },
}
let report = {
  query: 'song', count: 12, total: 12, version: 'v', sources: [],
  candidates: Array.from({ length: 12 }, (_, i) => ({ source_id: i % 2 ? 'b' : 'a', source_version: '1', item_id: String(i), title: 'Song', artist: 'Artist' })),
}
const api = {
  searchCandidates: async () => report,
  probeCandidateSizes(candidates, signal) {
    return new Promise((resolve, reject) => calls.push({ candidates, signal, resolve, reject }))
  },
  errorMessage: String,
}
const jsx = (type, props) => ({ type, props })
const exports = {}
const code = ts.transpileModule(readFileSync(new URL('../src/app/dashboard/search/page.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText
vm.runInNewContext(code, {
  exports, AbortController, URLSearchParams, window: { location: { search: '' } },
  require(name) {
    if (name === 'react') return hooks
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
    if (name === '@phosphor-icons/react') return {}
    if (name === '@/lib/api') return api
    if (name === '@/lib/format') return { formatBytes: (n) => `${n} bytes`, shortHash: String, formatDuration: String }
    throw new Error(name)
  },
})
function render() {
  for (let i = 0; i < 10; i++) {
    dirty = false; cursor = 0; tree = exports.default()
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
function find(predicate) { return nodes(tree).find(predicate) }
const toggle = () => find((n) => n.type === 'button' && 'aria-pressed' in n.props)
const tick = async () => { await new Promise((resolve) => setTimeout(resolve, 0)); render() }
render()
assert.equal(toggle(), undefined)
find((n) => n.type === 'input').props.onChange({ target: { value: 'song' } }); render()
await find((n) => n.type === 'form').props.onSubmit({ preventDefault() {} }); render()
assert.equal(toggle().props['aria-pressed'], false)
assert.equal(calls.length, 0, 'default search must not probe')
toggle().props.onClick(); render()
assert.equal(calls.length, 1)
assert.equal(calls[0].candidates.length, 10)
assert.deepEqual(Array.from(calls[0].candidates, (c) => c.item_id), Array.from({ length: 10 }, (_, i) => String(i)))
assert.ok(JSON.stringify(tree).includes('…'), 'pending rows show progress')
find((n) => n.type === 'select' && n.props['aria-label'] === '按音源筛选').props.onChange({ target: { value: 'b' } }); render()
assert.ok(calls[0].signal.aborted)
assert.equal(calls[1].candidates.length, 6)
assert.ok(calls[1].candidates.every((c) => c.source_id === 'b'))
calls[0].resolve({ b: { '1': { size: 99999 } } }); await tick()
assert.ok(!JSON.stringify(tree).includes('99999 bytes'), 'stale response ignored')
calls[1].resolve({ b: { '1': { size: 0 } } }); await tick()
assert.ok(JSON.stringify(tree).includes('0 bytes'), 'zero is a known size')
toggle().props.onClick(); render(); toggle().props.onClick(); render()
calls[2].reject(new Error('timeout')); await tick()
assert.ok(!nodes(tree).some((n) => n.type === 'span' && String(JSON.stringify(n.props.children)).includes('…')), 'failure clears progress')
assert.equal(nodes(tree).filter((n) => n.type === 'button' && n.props['aria-label'] === '下载到服务端媒体目录').length, 6)
console.log('search probe: opt-in, visible cap, cancellation, stale responses, zero size and failure passed')

report = { ...report, candidates: [
  { ...report.candidates[0], source_id: 'demo', item_id: '1' },
  { ...report.candidates[0], source_id: 'other', item_id: '1' },
  { ...report.candidates[0], source_id: 'demo:part', item_id: '1' },
  { ...report.candidates[0], source_id: 'demo', item_id: 'part:1' },
] }
await find((n) => n.type === 'form').props.onSubmit({ preventDefault() {} }); render()
assert.equal(calls.at(-1).candidates.length, 4)
calls.at(-1).resolve({ demo: { '1': { size: 22 }, 'part:1': { size: 44 } }, other: { '1': { size: 99 } }, 'demo:part': { '1': { size: 33 } } }); await tick()
for (const size of [22, 99, 33, 44]) {
  assert.ok(JSON.stringify(tree).includes(`${size} bytes`), `source-local identity preserves ${size} bytes`)
}
console.log('search probe: source-local IDs and delimiter collisions passed')

// Exercise the compiled shared client, including its CSRF and abort plumbing.
const { probeCandidateSizes } = await import('../.next/contract/api.mjs')
globalThis.window = { sessionStorage: { getItem: () => 'csrf-value' }, location: { pathname: '/dashboard/search' } }
globalThis.document = { cookie: '' }
let request
globalThis.fetch = async (url, options) => {
  request = { url, options }
  return new Response(JSON.stringify({ other: { '1': { size: 0, extension: 'mp3', media_type: 'audio/mpeg', quality: '320k' } } }), { headers: { 'Content-Type': 'application/json' } })
}
const controller = new AbortController()
const response = await probeCandidateSizes([report.candidates[1]], controller.signal)
assert.equal(request.url, '/admin/search/probe')
assert.equal(request.options.method, 'POST')
assert.equal(request.options.credentials, 'same-origin')
assert.equal(request.options.headers['x-csrf-token'], 'csrf-value')
assert.equal(request.options.signal, controller.signal)
assert.deepEqual(JSON.parse(request.options.body), { candidates: [report.candidates[1]] })
assert.equal(response.other['1'].size, 0)
console.log('search probe API: route, request body, CSRF, credentials and abort signal passed')
