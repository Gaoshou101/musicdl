/** Render the shared channel summary against distinct and missing evidence. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(import.meta.url)
const ts = require('typescript')
const exports = {}
const jsx = (type, props) => ({ type, props })
const code = ts.transpileModule(readFileSync(new URL('../src/components/ChannelMetrics.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText
vm.runInNewContext(code, {
  exports,
  require(name) {
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }
    throw new Error(name)
  },
})
function text(node) {
  if (Array.isArray(node)) return node.map(text).join('')
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'object') return text(node.props?.children)
  return String(node)
}
const summary = (row) => text(exports.default({ row }))
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
console.log('Channel metric rendering checks passed')
