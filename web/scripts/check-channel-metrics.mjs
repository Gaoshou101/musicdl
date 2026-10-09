/** Render the shared channel summary against distinct and missing evidence. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(import.meta.url)
const ts = require('typescript')
const jsx = (type, props) => ({ type, props })
const recentRequestsExports = loadComponent('../src/components/RecentRequests.tsx')
const recentRequestsComponent = recentRequestsExports.default
const exports = loadComponent('../src/components/ChannelMetrics.tsx')

function loadComponent(path) {
  const componentExports = {}
  const code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText
  vm.runInNewContext(code, {
    exports: componentExports,
    require(name) {
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
      if (name === '@/components/RecentRequests') return { __esModule: true, default: recentRequestsComponent }
      throw new Error(name)
    },
  })
  return componentExports
}

function resolve(node) {
  if (Array.isArray(node)) return node.map(resolve)
  if (node == null || typeof node !== 'object') return node
  if (typeof node.type === 'function') return resolve(node.type(node.props))
  if (!node.props) return node
  return { ...node, props: { ...node.props, children: resolve(node.props.children) } }
}

function text(node) {
  if (Array.isArray(node)) return node.map(text).join('')
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'object') return text(node.props?.children)
  return String(node)
}
function descendants(node, type) {
  if (Array.isArray(node)) return node.flatMap((child) => descendants(child, type))
  if (node == null || typeof node !== 'object') return []
  return [ ...(node.type === type ? [node] : []), ...descendants(node.props?.children, type) ]
}

const summary = (row) => text(resolve(exports.default({ row })))
assert.equal(summary(undefined), '')
assert.match(summary({}), /后端尚未提供分项统计/)
const empty = { metrics: {
  scope: 'process', window_size: 20,
  search: { samples: 0, successes: 0, failures: 0, rate: null },
  download: { samples: 0, successes: 0, failures: 0, excluded: 0, rate: null },
  quality: { samples: 0, fulfilled: 0, downgraded: 0, unknown: 0, rate: null },
  ranking: { eligible: false, minimum_samples: 5, score: 0 },
} }
assert.match(summary(empty), /搜索可用率 — · 0 次/)
assert.match(summary(empty), /来源尝试成功率 — · 0 次/)
assert.match(summary(empty), /无损请求兑现率 — · 可判定 0 次/)
assert.match(summary(empty), /样本不足 5 次/)
const mixed = structuredClone(empty)
Object.assign(mixed.metrics, {
  search: { samples: 20, successes: 20, failures: 0, rate: 1 },
  download: { samples: 5, successes: 0, failures: 5, excluded: 2, rate: 0 },
  quality: { samples: 4, fulfilled: 1, downgraded: 3, unknown: 2, rate: .25 },
  ranking: { eligible: true, minimum_samples: 5, score: -6 },
})
assert.match(summary(mixed), /搜索可用率 100% · 20 次/)
assert.match(summary(mixed), /来源尝试成功率 0% · 5 次/)
assert.match(summary(mixed), /无损请求兑现率 25% · 可判定 4 次/)
assert.match(summary(mixed), /降级 3 次/)
assert.match(summary(mixed), /音质未知 2 次/)
assert.match(summary(mixed), /本次运行 · 各取最近 20 次/)
assert.match(summary(mixed), /取消或本地原因 2 次/)
assert.match(summary(mixed), /下载样本已用于选源排序/)

const unavailable = resolve(recentRequestsComponent({ requests: undefined }))
assert.match(text(unavailable), /后端尚未提供近期请求记录/)
const noRequests = resolve(recentRequestsComponent({ requests: [] }))
assert.match(text(noRequests), /暂无近期请求/)
assert.equal(descendants(noRequests, 'span').length, 0)

const timestampText = (timestamp) => {
  const date = new Date(timestamp * 1000)
  const two = (part) => String(part).padStart(2, '0')
  return `${two(date.getMonth() + 1)}/${two(date.getDate())} ${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`
}
const fixture = Array.from({ length: 12 }, (_, index) => ({
  timestamp: 1_798_876_800 + index,
  kind: index % 2 === 0 ? 'search' : 'download',
  status: ['success', 'failure', 'unknown', 'excluded'][index % 4],
}))
const recent = resolve(recentRequestsComponent({ requests: fixture }))
const bars = descendants(recent, 'span')
assert.equal(bars.length, 10)
assert.equal(text(descendants(recent, 'p')[0]), timestampText(fixture[11].timestamp))
assert.equal(bars[0].props.style.backgroundColor, 'var(--color-neutral-400)')
assert.equal(bars[1].props.style.backgroundColor, 'transparent')
assert.equal(bars[1].props.style.border, '2px dashed var(--color-neutral-400)')
assert.equal(bars[2].props.style.backgroundColor, 'var(--color-success)')
assert.equal(bars[3].props.style.backgroundColor, 'var(--color-danger)')
assert.equal(bars[0].props.role, 'img')
assert.equal(bars[0].props.tabIndex, 0)
assert.equal(bars[0].props['aria-label'], `${timestampText(fixture[2].timestamp)} · 搜索结果未知`)
assert.match(bars[0].props.className, /focus-visible:outline/)
console.log('Channel metric rendering checks passed')
