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
  sourceImportDraft,
  sourceLanguageForFilename,
} = await import(`data:text/javascript,${encodeURIComponent(source)}`)

assert.deepEqual(parseSourceUrlLines('  https://a.test/source.js\n\nhttps://b.test/source.py  '), [
  'https://a.test/source.js',
  'https://b.test/source.py',
])
assert.equal(hasTooManySourceUrls('a\nb\nc', 2), true)
assert.equal(sourceLanguageForFilename('SOURCE.PY'), 'python')
assert.equal(sourceLanguageForFilename('source.mjs'), 'javascript')

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

console.log('source import queue harness: 14 checks passed')
