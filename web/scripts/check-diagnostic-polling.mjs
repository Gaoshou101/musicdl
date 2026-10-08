/** Exercise source-diagnostics polling through the page and shared polling hook. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(import.meta.url)
const ts = require('typescript')
const jsx = (type, props, key) => ({ type, props: { ...props, key } })
const jsxs = jsx
const pageSource = readFileSync(new URL('../src/app/dashboard/sources/page.tsx', import.meta.url), 'utf8')
const pollingSource = readFileSync(new URL('../src/lib/usePolling.ts', import.meta.url), 'utf8')
const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8')
const transpile = (source) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText

const same = (left, right) => left === right || Boolean(
  left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index])),
)

const source = { id: 'demo', enabled: true, priority: 0, timeout: 10, name: 'Demo', plugin: null }
const result = (sourceId, status, testedAt) => ({
  source_id: sourceId,
  source_version: '1',
  fingerprint: null,
  status,
  stage: 'resolve',
  message: status,
  query: 'query',
  actual_query: 'query',
  queries_tried: ['query'],
  tested_at: testedAt,
  search_ms: 1,
  resolve_ms: 1,
  extension: null,
  quality: null,
  download_verified: false,
  stale: false,
})
const job = (id, sourceId) => ({
  job_id: id,
  source_id: sourceId,
  source_version: '1',
  fingerprint: null,
  status: 'running',
  stage: 'search',
  query: 'query',
  submitted_at: 100,
  deadline_at: 200,
  batch_id: null,
})
const snapshot = (jobs = [], results = []) => ({
  jobs,
  results,
  active: jobs.filter((item) => item.status === 'running').length,
  active_limit: 2,
  pending: jobs.filter((item) => item.status === 'queued').length,
  pending_limit: 32,
})

function createHarness({ snapshots, startResult = { mode: 'scheduled', job_id: 'job-new' } }) {
  let cursor = 0
  let dirty = false
  let unmounted = false
  let lateStateUpdates = 0
  const slots = []
  const effects = []
  const timers = new Map()
  const documentListeners = new Map()
  const diagnosticCalls = []
  const jobCalls = []
  let nextTimerId = 0
  let nextSnapshot = 0
  let startCalls = 0

  const hooks = {
    useRef(value) {
      const index = cursor++
      slots[index] ??= { current: value }
      return slots[index]
    },
    useState(initial) {
      const index = cursor++
      slots[index] ??= { value: typeof initial === 'function' ? initial() : initial }
      return [slots[index].value, (update) => {
        const previous = slots[index].value
        const value = typeof update === 'function' ? update(previous) : update
        if (Object.is(previous, value)) return
        if (unmounted) lateStateUpdates += 1
        slots[index].value = value
        dirty = true
      }]
    },
    useCallback(callback, dependencies) {
      const index = cursor++
      if (!same(slots[index]?.dependencies, dependencies)) slots[index] = { callback, dependencies }
      return slots[index].callback
    },
    useMemo(factory, dependencies) {
      const index = cursor++
      if (!same(slots[index]?.dependencies, dependencies)) slots[index] = { value: factory(), dependencies }
      return slots[index].value
    },
    useEffect(create, dependencies) {
      const index = cursor++
      if (!same(slots[index]?.dependencies, dependencies)) {
        const cleanup = slots[index]?.cleanup
        slots[index] = { dependencies, cleanup: undefined }
        effects.push(() => {
          cleanup?.()
          slots[index].cleanup = create()
        })
      }
    },
  }

  const document = {
    hidden: false,
    addEventListener: (name, callback) => documentListeners.set(name, callback),
    removeEventListener: (name) => documentListeners.delete(name),
  }
  const setTimeoutFake = (callback, delay) => {
    const id = ++nextTimerId
    timers.set(id, { callback, delay })
    return id
  }
  const clearTimeoutFake = (id) => timers.delete(id)
  const pendingSnapshot = () => snapshots[Math.min(nextSnapshot++, snapshots.length - 1)]
  const api = {
    listSources: async () => ({ items: [source] }),
    listSourceHealth: async () => ({ sources: [] }),
    sourceDiagnostics: (signal) => {
      diagnosticCalls.push(signal)
      const next = pendingSnapshot()
      return typeof next === 'function' ? next(signal) : Promise.resolve(next)
    },
    startSourceDiagnostic: async () => { startCalls += 1; return startResult },
    startSourceDiagnosticBatch: async () => ({ batch_id: 'batch', requested: 0, scheduled: 0,
      deduplicated: 0, cooldown: 0, not_started: 0, items: [] }),
    sourceDiagnosticJob: async (id) => { jobCalls.push(id); throw new Error('per-job polling is disabled') },
    cancelSourceDiagnosticJob: async () => ({ cancelled: true, status: 'cancelled' }),
    errorMessage: (error) => String(error),
    reloadNote: () => '',
    analyzeSource: async () => ({}),
    checkSourceLossless: async () => ({ status: 'unknown' }),
    deleteSource: async () => ({}),
    fetchSource: async () => ({}),
    installSource: async () => ({}),
    updateSource: async () => source,
  }
  const importHelpers = {
    NO_IMPORT_GRANTS: { allow_insecure_http: false, allow_ip_hosts: false,
      allow_any_host: false, allowed_ports: null },
    SOURCE_IMPORT_MAX_BYTES: 256 * 1024,
    SOURCE_IMPORT_MAX_URLS: 12,
    duplicateSourceIds: () => new Set(),
    hasTooManySourceUrls: () => false,
    isSupportedSourceFilename: () => true,
    importRowIsInstallable: () => false,
    installSourceQueueSequentially: async () => [],
    parseSourceUrlLines: () => [],
    requiredGrantKeys: () => [],
    sourceImportDraft: () => ({}),
    sourceLanguageForFilename: () => 'javascript',
  }
  const icons = new Proxy({}, { get: () => function Icon() {} })
  const pollingExports = {}
  vm.runInNewContext(transpile(pollingSource), {
    exports: pollingExports,
    require: (name) => name === 'react' ? hooks : (() => { throw new Error(name) })(),
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    document,
    AbortController,
  })

  const pageExports = {}
  vm.runInNewContext(transpile(pageSource), {
    exports: pageExports,
    require(name) {
      if (name === 'react') return hooks
      if (name === 'react/jsx-runtime') return { jsx, jsxs }
      if (name === '@phosphor-icons/react') return icons
      if (name === '@/lib/api') return api
      if (name === '@/lib/format') return { formatBytes: String }
      if (name === '@/lib/usePolling') return pollingExports
      if (name === '@/components/ChannelMetrics') return { __esModule: true, default: () => null }
      if (name === '@/lib/sourceImport') return importHelpers
      throw new Error(name)
    },
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    document,
    window: { confirm: () => false },
    AbortController,
  })

  let tree
  function render() {
    for (let pass = 0; pass < 10; pass += 1) {
      dirty = false
      cursor = 0
      tree = pageExports.default()
      for (const effect of effects.splice(0)) effect()
      if (!dirty) return
    }
    throw new Error('component render loop')
  }
  async function settle() {
    for (let pass = 0; pass < 20; pass += 1) {
      await Promise.resolve()
      render()
    }
  }
  async function runNextTimer() {
    const first = timers.entries().next().value
    assert.ok(first, 'expected one scheduled polling cycle')
    timers.delete(first[0])
    first[1].callback()
    await settle()
  }
  function unmount() {
    unmounted = true
    for (const slot of slots) {
      slot?.cleanup?.()
      if (slot) slot.cleanup = undefined
    }
  }
  function text(node) {
    if (Array.isArray(node)) return node.map(text).join('')
    if (node == null || typeof node === 'boolean') return ''
    if (typeof node === 'object') return text(node.props?.children)
    return String(node)
  }
  function descendants(node) {
    if (Array.isArray(node)) return node.flatMap(descendants)
    if (!node || typeof node !== 'object') return []
    return [node, ...descendants(node.props?.children)]
  }

  return {
    api, diagnosticCalls, jobCalls, timers, documentListeners,
    runNextTimer, render, settle, unmount, text, descendants,
    get tree() { return tree },
    get lateStateUpdates() { return lateStateUpdates },
    get startCalls() { return startCalls },
  }
}

async function checkApiForwardsAbortSignal() {
  let seenOptions
  const apiExports = {}
  vm.runInNewContext(transpile(apiSource), {
    exports: apiExports,
    fetch: async (_url, options) => {
      seenOptions = options
      return { ok: true, headers: { get: () => 'application/json' }, text: async () => JSON.stringify(snapshot()) }
    },
  })
  const controller = new AbortController()
  await apiExports.sourceDiagnostics(controller.signal)
  assert.equal(seenOptions.signal, controller.signal, 'snapshot fetch forwards the polling abort signal')
}

await checkApiForwardsAbortSignal()

const thirtyTwoJobs = Array.from({ length: 32 }, (_, index) => job(`job-${index}`, `source-${index}`))
let finishSlowSnapshot
let abortedSlowSignal
let abortCount = 0
const slow = createHarness({ snapshots: [
  snapshot(thirtyTwoJobs),
  (signal) => new Promise((resolve) => {
    finishSlowSnapshot = resolve
    abortedSlowSignal = signal
    signal.addEventListener('abort', () => { abortCount += 1 }, { once: true })
  }),
] })
slow.render()
await slow.settle()
assert.equal(slow.diagnosticCalls.length, 1, 'initial load obtains one snapshot for all 32 jobs')
assert.equal(slow.jobCalls.length, 0, 'active jobs do not trigger per-job requests')
assert.equal(slow.timers.size, 1)
await slow.runNextTimer()
assert.equal(slow.diagnosticCalls.length, 2, 'one timer cycle issues one shared snapshot request')
assert.equal(slow.timers.size, 0, 'a slow request cannot overlap with a later polling cycle')
slow.unmount()
assert.equal(abortedSlowSignal.aborted, true, 'navigation cleanup aborts the in-flight snapshot')
assert.equal(abortCount, 1)
const lateUpdatesBeforeResolve = slow.lateStateUpdates
finishSlowSnapshot(snapshot([], [result('demo', 'auth_required', 201)]))
for (let pass = 0; pass < 20; pass += 1) await Promise.resolve()
assert.equal(slow.lateStateUpdates, lateUpdatesBeforeResolve, 'an aborted response cannot update an unmounted page')
assert.equal(slow.timers.size, 0, 'unmount leaves no detached polling timer')

let terminalSnapshots = [snapshot([job('active', 'demo')])]
for (const [index, status] of ['busy', 'auth_required', 'rate_limited', 'timeout'].entries()) {
  terminalSnapshots.push(snapshot([job('active', 'demo')], [
    ...terminalSnapshots.at(-1).results,
    result(`terminal-${status}`, status, 300 + index),
  ]))
}
terminalSnapshots.push(snapshot([], terminalSnapshots.at(-1).results))
const terminal = createHarness({ snapshots: terminalSnapshots })
terminal.render()
await terminal.settle()
for (const status of ['busy', 'auth_required', 'rate_limited', 'timeout']) {
  await terminal.runNextTimer()
  const notice = terminal.text(terminal.tree)
  const expected = {
    busy: '未检查：下载容量已满',
    auth_required: '上游需要授权',
    rate_limited: '上游正在限流',
    timeout: '检查超时',
  }[status]
  assert.ok(notice.includes(expected), `snapshot completion preserves the ${status} notice`)
  assert.equal(terminal.timers.size, 1, `${status} snapshot still has an active job`)
}
await terminal.runNextTimer()
assert.equal(terminal.timers.size, 0, 'polling stops after the snapshot becomes idle')
assert.equal(terminal.diagnosticCalls.length, 6, 'one final snapshot observes idle, then polling stops')

const actionSnapshot = snapshot([job('job-new', 'demo')])
const action = createHarness({ snapshots: [snapshot(), actionSnapshot] })
action.render()
await action.settle()
const diagnoseButton = action.descendants(action.tree).find((node) => node.props?.['aria-label'] === '诊断 Demo')
assert.ok(diagnoseButton, 'source diagnostic action is rendered')
diagnoseButton.props.onClick()
await action.settle()
assert.equal(action.startCalls, 1)
assert.equal(action.diagnosticCalls.length, 2, 'starting one job requests exactly one post-action snapshot')
assert.equal(action.timers.size, 1, 'the action starts one shared poll schedule')

console.log('Source diagnostics polling lifecycle checks passed')
