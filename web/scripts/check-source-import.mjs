import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const output = fileURLToPath(new URL('../.next/source-import-contract/sourceImport.js', import.meta.url))
const source = await readFile(output, 'utf8')
const {
  duplicateSourceIds,
  hasTooManySourceUrls,
  installSourceQueueSequentially,
  importRowIsInstallable,
  parseSourceUrlLines,
  queueInstallableRows,
  runSourceImportUrlBatch,
  SOURCE_LIST_FORMAT,
  sourceImportDraft,
  sourceLanguageForFilename,
} = await import(`data:text/javascript,${encodeURIComponent(source)}`)

const apiPath = fileURLToPath(new URL('../.next/source-import-contract/api.js', import.meta.url))
const apiSource = await readFile(apiPath, 'utf8')
const api = await import(`data:text/javascript,${encodeURIComponent(apiSource)}`)

assert.deepEqual(parseSourceUrlLines('  https://a.test/source.js\n\nhttps://b.test/source.py  '), [
  'https://a.test/source.js',
  'https://b.test/source.py',
])
assert.equal(hasTooManySourceUrls('a\nb\nc', 2), true)
assert.equal(sourceLanguageForFilename('SOURCE.PY'), 'python')
assert.equal(sourceLanguageForFilename('source.mjs'), 'javascript')

const catalogUrl = 'https://raw.githubusercontent.com/Gaoshou101/musicdl/main/sources/catalog.json'
const catalog = JSON.parse(await readFile(new URL('../../sources/catalog.json', import.meta.url), 'utf8'))
const catalogReadme = await readFile(new URL('../../sources/README.md', import.meta.url), 'utf8')
const tableUrls = [...catalogReadme.matchAll(/\|\s+\[[^\]]+\]\((https:\/\/raw\.githubusercontent\.com\/[^)]+)\)\s+\|/g)]
  .map((match) => match[1])
assert.equal(catalog.format, SOURCE_LIST_FORMAT)
assert.equal(catalog.sources.length, 12)
assert.deepEqual(catalog.sources.map(({ url }) => url), tableUrls)

const sourceList = { kind: 'source_list', format: SOURCE_LIST_FORMAT, sources: catalog.sources }
const directReport = { script: 'export default 1', filename: 'direct.js', language: 'javascript' }
const acceptedPlans = []
const acceptedChildren = []
const acceptedTopLevel = []
let activeTopLevel = 0
let maxActiveTopLevel = 0
const acceptedStatus = await runSourceImportUrlBatch(
  [catalogUrl], 8,
  async (url) => {
    acceptedTopLevel.push(url)
    activeTopLevel += 1
    maxActiveTopLevel = Math.max(maxActiveTopLevel, activeTopLevel)
    await Promise.resolve()
    activeTopLevel -= 1
    return sourceList
  },
  async (item) => {
    if (item.report) throw new Error('catalog children must use script-only fetches')
    assert.equal(item.topLevelInput, false)
    acceptedChildren.push(item.url)
  },
  () => true,
  (items) => acceptedPlans.push(...items),
  () => assert.fail('catalog children unexpectedly failed'),
)
assert.equal(acceptedStatus, 'complete')
assert.equal(8 + acceptedPlans.length, 20)
assert.deepEqual(acceptedTopLevel, [catalogUrl])
assert.equal(maxActiveTopLevel, 1)
assert.deepEqual(acceptedChildren, catalog.sources.map(({ url }) => url))

let overflowAppended = false
let overflowChildFetches = 0
const overflowStatus = await runSourceImportUrlBatch(
  [catalogUrl], 9, async () => sourceList,
  async () => { overflowChildFetches += 1 },
  () => true,
  () => { overflowAppended = true },
  () => assert.fail('an unappended child cannot fail'),
)
assert.equal(overflowStatus, 'overflow')
assert.equal(overflowAppended, false)
assert.equal(overflowChildFetches, 0)

const mixedOrder = []
const mixedStatus = await runSourceImportUrlBatch(
  ['https://example.test/direct.js', catalogUrl], 7,
  async (url) => {
    mixedOrder.push('top:' + url)
    return url === catalogUrl ? sourceList : directReport
  },
  async (item) => mixedOrder.push(item.report ? 'script:' + item.url : 'child:' + item.url),
  () => true,
  (items) => assert.equal(items.length + 7, 20),
  () => assert.fail('mixed inputs unexpectedly failed'),
)
assert.equal(mixedStatus, 'complete')
assert.deepEqual(mixedOrder.slice(0, 3), [
  'top:https://example.test/direct.js',
  'top:' + catalogUrl,
  'script:https://example.test/direct.js',
])
assert.deepEqual(mixedOrder.slice(3), catalog.sources.map(({ url }) => 'child:' + url))

let failedTopRows = []
let processedFailedTop = 0
const failedTopStatus = await runSourceImportUrlBatch(
  ['https://example.test/offline.js'], 19,
  async () => { throw new Error('offline') },
  async () => { processedFailedTop += 1 },
  () => true,
  (items) => { failedTopRows = [...items] },
  (item, index, error) => {
    assert.equal(index, 0)
    assert.equal(item.topLevelInput, true)
    assert.equal(error.message, 'offline')
  },
)
assert.equal(failedTopStatus, 'complete')
assert.equal(failedTopRows.length, 1)
assert.equal(processedFailedTop, 0)

const partialChildren = []
const partialFailures = []
const partialListStatus = await runSourceImportUrlBatch(
  ['https://example.test/list.json'], 0,
  async () => ({ ...sourceList, sources: sourceList.sources.slice(0, 3) }),
  async (item) => {
    partialChildren.push(item.url)
    if (item.url === catalog.sources[1].url) throw new Error('child unavailable')
  },
  () => true,
  () => {},
  (item, index, error) => partialFailures.push({ url: item.url, index, message: error.message }),
)
assert.equal(partialListStatus, 'complete')
assert.equal(partialChildren.length, 3)
assert.deepEqual(partialFailures, [{ url: catalog.sources[1].url, index: 1, message: 'child unavailable' }])

let currentGeneration = true
let finishTopLevel
let staleAppend = false
const cancelledPlan = runSourceImportUrlBatch(
  [catalogUrl], 0,
  () => new Promise((resolve) => { finishTopLevel = resolve }),
  async () => assert.fail('cancelled plan must not process children'),
  () => currentGeneration,
  () => { staleAppend = true },
  () => assert.fail('cancelled plan must not report a row failure'),
)
await Promise.resolve()
currentGeneration = false
finishTopLevel(sourceList)
assert.equal(await cancelledPlan, 'cancelled')
assert.equal(staleAppend, false)

let retryCount = 0
const failedRetry = await runSourceImportUrlBatch(
  ['https://example.test/retry.js'], 0,
  async () => { retryCount += 1; throw new Error('temporary network failure') },
  async () => assert.fail('failed top-level fetch has no script to process'),
  () => true,
  () => {},
  () => {},
)
assert.equal(failedRetry, 'complete')
const successfulRetry = await runSourceImportUrlBatch(
  ['https://example.test/retry.js'], 0,
  async () => { retryCount += 1; return directReport },
  async (item) => assert.equal(item.report, directReport),
  () => true,
  () => {},
  () => assert.fail('retried top-level fetch unexpectedly failed'),
)
assert.equal(successfulRetry, 'complete')
assert.equal(retryCount, 2)

const apiCalls = []
const originalFetch = globalThis.fetch
globalThis.fetch = async (_input, init) => {
  apiCalls.push(JSON.parse(init.body))
  return new Response(JSON.stringify(directReport), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}
try {
  await api.fetchSourceInput(catalogUrl)
  await api.fetchSource(catalog.sources[0].url)
} finally {
  globalThis.fetch = originalFetch
}
assert.deepEqual(apiCalls, [
  { url: catalogUrl, allow_source_list: true },
  { url: catalog.sources[0].url },
])

const preview = { installable: true }
const row = (key, id, status = 'ready') => ({
  key,
  url: `https://${key}.test/source.js`,
  script: `export default '${key}'`,
  filename: 'source.js',
  language: 'javascript',
  id,
  grants: {
    allow_insecure_http: false,
    allow_ip_hosts: false,
    allow_any_host: false,
    allowed_ports: null,
  },
  preview,
  status,
  error: null,
  result: null,
  reload: null,
  existing: false,
})

const first = row('first', 'demo')
const second = row('second', 'demo')
const failed = row('failed', 'other', 'failed')
assert.deepEqual([...duplicateSourceIds([first, second])], ['demo'])
assert.equal(importRowIsInstallable(first, new Set()), true)
assert.equal(importRowIsInstallable(failed, new Set()), false)
assert.deepEqual(queueInstallableRows([first, second, failed]), [])
assert.deepEqual(queueInstallableRows([first, row('third', 'third')]).map(({ id }) => id), ['demo', 'third'])

const draft = sourceImportDraft({
  ...row('draft', 'custom'),
  filename: 'custom.py',
  language: 'python',
  grants: {
    allow_insecure_http: true,
    allow_ip_hosts: false,
    allow_any_host: true,
    allowed_ports: [80, 443],
  },
})
assert.deepEqual(draft, {
  id: 'custom',
  script: "export default 'draft'",
  filename: 'custom.py',
  language: 'python',
  allow_insecure_http: true,
  allow_ip_hosts: undefined,
  allow_any_host: true,
  allowed_ports: [80, 443],
})
const defaultGrantDraft = sourceImportDraft({ ...row('default-grants', 'default'), grants: {
  allow_insecure_http: false,
  allow_ip_hosts: false,
  allow_any_host: false,
  allowed_ports: null,
} })
assert.equal(defaultGrantDraft.allow_insecure_http, undefined)
assert.equal(defaultGrantDraft.allow_ip_hosts, undefined)
assert.equal(defaultGrantDraft.allow_any_host, undefined)
assert.equal(defaultGrantDraft.allowed_ports, undefined)

const installRows = [row('install-one', 'one'), row('install-two', 'two')]
const installState = new Map(installRows.map((item) => [item.key, item]))
const installOrder = []
let activeInstalls = 0
let maxActiveInstalls = 0
let failSecond = true
const updateInstallRow = (item, patch) => {
  installState.set(item.key, { ...installState.get(item.key), ...patch })
}
const fakeInstall = async (draft) => {
  installOrder.push(draft.id)
  activeInstalls += 1
  maxActiveInstalls = Math.max(maxActiveInstalls, activeInstalls)
  await Promise.resolve()
  activeInstalls -= 1
  if (draft.id === 'two' && failSecond) {
    throw new Error('temporary install failure')
  }
  return { id: draft.id, enabled: true, priority: 0, timeout: 10, name: null, plugin: null }
}

const partial = await installSourceQueueSequentially(
  [...installState.values()], fakeInstall, updateInstallRow,
  (error) => error instanceof Error ? error.message : String(error),
)
assert.deepEqual(installOrder, ['one', 'two'])
assert.equal(maxActiveInstalls, 1)
assert.deepEqual(partial, { attempted: 2, installed: 1, failed: 1 })
assert.equal(installState.get('install-one').status, 'installed')
assert.equal(installState.get('install-two').status, 'ready')
assert.equal(installState.get('install-two').error, 'temporary install failure')

failSecond = false
const retry = await installSourceQueueSequentially(
  [...installState.values()], fakeInstall, updateInstallRow,
  (error) => error instanceof Error ? error.message : String(error),
)
assert.deepEqual(installOrder, ['one', 'two', 'two'])
assert.deepEqual(retry, { attempted: 1, installed: 1, failed: 0 })
assert.equal(installState.get('install-two').status, 'installed')
assert.equal(installState.get('install-two').error, null)

console.log('source import queue harness: all assertions passed')
