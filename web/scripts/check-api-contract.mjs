/**
 * Drives the compiled admin client (src/lib/api.ts) against a running panel.
 *
 * This is the check that the panel's own request shapes -- the /api hop, the
 * CSRF header, the envelopes it reads back -- are the ones the real backend
 * accepts. It runs in Node with a cookie jar standing in for a browser, so the
 * double-submit pair is exercised exactly as it will be in a browser.
 *
 * Usage:
 *   npm run check:api:build
 *   node scripts/check-api-contract.mjs <panel-origin> <suite> [user] [password] [script]
 *
 *   suite=remote         read-only flow against a deployed panel (never mutates)
 *   suite=local          full flow against an app whose plugin store is present
 *                        but which runs no plugin runner
 *   suite=local-nostore  the same app started with no plugin store at all
 *
 * The three deployments are started like this, all from the repository root:
 *
 *   # the panel itself, serving /api/* from the app on 127.0.0.1:8000
 *   cd web && npm run build && npx next start -p 3101
 *
 *   # suite=local   -- store present, no runner: search answers an empty
 *   #                  catalogue, health reports the runner as failed
 *   mkdir -p "$ROOT/app" "$ROOT/media" "$ROOT/telegram"
 *   MUSICDL_MEDIA__ROOT=$ROOT/media MUSICDL_TELEGRAM__SESSION_ROOT=$ROOT/telegram \
 *   MUSICDL_PLUGIN__APP_DATA_ROOT=$ROOT/app MUSICDL_ADMIN__STATE_PATH=$ROOT/admin-state.json \
 *     .venv/Scripts/python -m uvicorn musicdl.app:app --host 127.0.0.1 --port 8000
 *
 *   # suite=local-nostore -- point MUSICDL_PLUGIN__APP_DATA_ROOT at a leaf that
 *   #                        does not exist; the portal then has no code store
 *   #                        and refuses a search with 503
 *
 * A fresh app starts on the default admin/password pair, and both local suites
 * change it to the password below, because a panel still on the default
 * credentials refuses every read on purpose.
 */
import { readFileSync } from 'node:fs'

const base = (process.argv[2] ?? 'http://127.0.0.1:3101').replace(/\/+$/, '')
const suite = process.argv[3] ?? 'remote'
const username = process.argv[4] ?? 'admin'
const password = process.argv[5] ?? 'password'

// The client is TypeScript, so it is compiled next to the build before it can
// be imported here; `npm run check:api:build` is what puts it there.
const compiled = new URL('../.next/contract/api.mjs', import.meta.url)

process.env.NEXT_PUBLIC_API_BASE = base

const jar = new Map()
let lastSetCookies = []
const cookieHeader = () => [...jar.entries()].map(([name, value]) => `${name}=${value}`).join('; ')

const realFetch = globalThis.fetch
globalThis.fetch = async (input, init = {}) => {
  const url = typeof input === 'string' ? input : String(input?.url ?? input)
  const headers = new Headers(init.headers ?? {})
  if (jar.size && url.startsWith(base)) headers.set('cookie', cookieHeader())
  const response = await realFetch(input, { ...init, headers })
  const raw = response.headers.getSetCookie?.() ?? []
  lastSetCookies = raw
  for (const item of raw) {
    const [pair] = item.split(';')
    const index = pair.indexOf('=')
    if (index > 0) jar.set(pair.slice(0, index).trim(), pair.slice(index + 1).trim())
  }
  return response
}

globalThis.document = {
  get cookie() {
    return cookieHeader()
  },
}

const api = await import(compiled.href)

let failures = 0
const results = []

function check(label, condition, detail = '') {
  results.push(`${condition ? 'PASS' : 'FAIL'}  ${label}${detail ? `  ${detail}` : ''}`)
  if (!condition) failures += 1
}

async function expectApiError(label, run, status, messagePart = '') {
  try {
    await run()
    check(label, false, 'expected a failure, got a success')
  } catch (error) {
    const isApi = error instanceof api.ApiError
    check(
      label,
      isApi && error.status === status && (!messagePart || error.message.includes(messagePart)),
      isApi ? `${error.status} ${error.message}` : String(error),
    )
  }
}

// Both local suites run against a freshly started app, so both have to walk it
// off the default credentials before anything else will answer.
const freshApp = suite === 'local' || suite === 'local-nostore'
const ROTATED_PASSWORD = 'check-password-2026'

async function main() {
  // The unauthenticated shapes first: a signed-out browser must be told so, and
  // a write without the double-submit pair must be refused.
  await expectApiError('unauthenticated read is refused', () => api.listSources(), 401, '登录状态已失效')

  // A second run against the same app meets the password the first run set, not
  // the default one. That is a deployment an operator could really have, so it
  // is walked rather than reported as a failure.
  let login = null
  let rotated = false
  try {
    login = await api.login(username, password)
  } catch (error) {
    if (!freshApp) throw error
    login = await api.login(username, ROTATED_PASSWORD)
    rotated = true
  }
  check('login sets a session cookie through the /api proxy', jar.has('admin_session'), [...jar.keys()].join(','))
  check('login sets the csrf cookie the backend will compare against', jar.has('csrf_token'))
  check('login reply carried a csrf token', Boolean(api.readCookie('csrf_token')))
  check('login reports whether the password must change', typeof login.must_change === 'boolean', `must_change=${login.must_change}`)

  if (freshApp && rotated) {
    // The credentials were rotated by an earlier run; what still has to hold is
    // that the session the rotated pair hands back can read the panel.
    const sourcesBefore = await api.listSources()
    check('a rotated deployment keeps working', Array.isArray(sourcesBefore.items))
  } else if (freshApp) {
    // A deployment still on the default password may only reach the credential
    // change; every other read is refused with the message the panel turns into
    // a redirect to the settings page.
    await expectApiError('default credentials block other reads', () => api.listSources(), 403, '请先修改默认密码')
    await api.changeCredentials({ username, password, newPassword: ROTATED_PASSWORD })
    const afterChange = await api.listSources()
    check('credentials changed and the replacement session works', Array.isArray(afterChange.items))
  }

  const bare = await fetch(`${base}/api/bots`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: 'csrf-probe', username: 'csrfprobe_bot' }),
  })
  check('a write without the csrf header is refused', bare.status === 403, `status=${bare.status}`)

  const sources = await api.listSources()
  check('source list unwraps the items envelope', Array.isArray(sources.items), `${sources.items.length} 个音源`)

  const health = await api.readHealth()
  check('health reports the four checks', Object.keys(health.checks).length === 4, JSON.stringify(health))

  // The panel's own settings layer: every group it owns is listed, and a
  // secret is described without ever carrying a value back.
  const config = await api.listConfig()
  check('config lists the groups the panel owns', config.groups.length >= 6, `${config.groups.length} 组`)
  const configFields = config.groups.flatMap((group) => group.fields)
  const secrets = configFields.filter((field) => field.secret)
  check(
    'a secret is reported as set or unset, never as a value',
    secrets.length > 0 && secrets.every((field) => field.value === null && typeof field.set === 'boolean'),
    `${secrets.length} 只密钥`,
  )
  check(
    'the fields only the deployment owns are listed apart',
    config.container.length > 0 && config.container.every((knob) => typeof knob.label === 'string'),
    `${config.container.length} 项`,
  )

  if (suite === 'remote') {
    const search = await api.searchCandidates('晴天', 5)
    check('search answers with candidates', Array.isArray(search.candidates), `${search.count}/${search.total} 条，${search.sources.length} 个音源`)

    // A source that cannot resolve one recording is normal (a platform may want
    // an account), so the check walks the list until one candidate downloads and
    // reports what the failures said rather than stopping at the first.
    let download = null
    const refusals = []
    for (const candidate of search.candidates.slice(0, 8)) {
      try {
        download = await api.downloadCandidate(candidate)
        break
      } catch (error) {
        refusals.push(`${candidate.source_id}: ${error.message}`)
      }
    }
    check(
      'download accepts the candidate the search listed',
      Boolean(download?.sha256 && download?.relative_path),
      download ? `${download.size_bytes} 字节 ${download.media_type}` : refusals.join(' | '),
    )

    if (download) {
      const media = await fetch(api.mediaUrl(download.relative_path))
      const body = new Uint8Array(await media.arrayBuffer())
      check(
        'media is served back through the proxy',
        media.status === 200 && body.byteLength === download.size_bytes,
        `${media.status} ${body.byteLength}/${download.size_bytes} 字节`,
      )
    }
  } else if (suite === 'local') {
    // This deployment has a code store but starts no plugin runner. An empty
    // store is an honest empty catalogue, not a failure, and the one thing it
    // must not do is claim to be healthy while nothing can fetch media.
    const empty = await api.searchCandidates('晴天', 5)
    check('an empty catalogue answers without failing', Array.isArray(empty.candidates) && empty.total === 0, `${empty.count}/${empty.total} 条`)
    check('health does not call a runner-less deployment healthy', health.status === 'degraded' && health.checks.plugin_runner !== 'ok', `status=${health.status} plugin_runner=${health.checks.plugin_runner}`)
  } else {
    // No plugin store in this deployment: the portal has to say so in the one
    // sentence the panel translates, not fail halfway through a request.
    await expectApiError('search reports an absent runtime', () => api.searchCandidates('晴天', 5), 503, '搜索运行环境不可用')
  }

  const events = await api.readEvents(0, 5)
  check('events unwrap the items envelope', Array.isArray(events.items), `total=${events.total}`)
  const audit = await api.readAudit(0, 5)
  check('audit unwraps the items envelope', Array.isArray(audit.items), `total=${audit.total}`)

  if (suite === 'local') {
    const created = await api.createBot({ id: 'check-bot', username: 'checkbot_one', priority: 5, timeout: 9.5 })
    check('bot create round-trips', created.username === 'checkbot_one' && created.priority === 5, JSON.stringify(created))

    // A hot setting is adopted by the running process and reported as coming
    // from the panel's layer; a null hands the field back to the deployment.
    const flatten = (report) => report.groups.flatMap((group) => group.fields)
    const raised = await api.updateConfig({ 'admin.login_limit': 7 })
    const limiter = flatten(raised).find((field) => field.key === 'admin.login_limit')
    check('a hot setting round-trips through the panel layer', limiter?.value === 7 && limiter?.source === 'panel', JSON.stringify(limiter))

    const restored = await api.updateConfig({ 'admin.login_limit': null })
    const cleared = flatten(restored).find((field) => field.key === 'admin.login_limit')
    check('a null hands the field back to the deployment', cleared?.source !== 'panel', JSON.stringify(cleared))

    await expectApiError('an unknown setting is refused', () => api.updateConfig({ 'nope.nope': 1 }), 422, '未知配置项')

    const updated = await api.updateBot('check-bot', { enabled: false, command_template: '/search {query}' })
    check('bot update round-trips', updated.enabled === false && updated.command_template === '/search {query}')
    const botList = await api.listBots()
    check('bot list unwraps items', botList.items.some((item) => item.id === 'check-bot'))
    await expectApiError('an invalid bot username is refused', () => api.createBot({ id: 'bad-bot', username: 'no' }), 422, 'Bot 用户名无效')
    await api.deleteBot('check-bot')
    const afterDelete = await api.listBots()
    check('bot delete round-trips', !afterDelete.items.some((item) => item.id === 'check-bot'))

    const scriptPath = process.argv[6] ?? '../tests/fixtures/plugins/lx_http_source.js'
    const script = readFileSync(scriptPath, 'utf8')
    const filename = scriptPath.split(/[\\/]/).pop()
    const firstPreview = await api.analyzeSource({ id: 'check-lx-source', script, filename })
    check(
      'analyze previews an lx script without storing it',
      typeof firstPreview.installable === 'boolean',
      `install_path=${firstPreview.install_path} 需要授权=${Object.keys(firstPreview.required_grants ?? {}).join(',') || '无'}`,
    )

    const grants = {}
    for (const key of Object.keys(firstPreview.required_grants ?? {})) {
      grants[key] = key === 'allowed_ports' ? (firstPreview.analysis?.allowed_ports ?? [443]) : true
    }
    const granted = await api.analyzeSource({ id: 'check-lx-source', script, filename, ...grants })
    check('granting exactly what the script declares makes it installable', granted.installable, granted.refusal ?? '')

    const installed = await api.installSource({ id: 'check-lx-source', script, filename, ...grants })
    check('install stores the script with an egress policy', Boolean(installed.plugin?.sha256), JSON.stringify(installed.plugin?.egress))

    const patched = await api.updateSource('check-lx-source', { enabled: false, priority: 7, timeout: 12, name: '检查用音源' })
    check('source update round-trips', patched.enabled === false && patched.priority === 7 && patched.name === '检查用音源')
    await expectApiError('an out-of-range priority is refused', () => api.updateSource('check-lx-source', { priority: 5000 }), 422, '优先级')

    const removedSource = await api.deleteSource('check-lx-source')
    check('source delete reports what it uninstalled', Array.isArray(removedSource.uninstalled), `uninstalled=${removedSource.uninstalled.length}`)
  }
}

try {
  await main()
} catch (error) {
  check('run finished without an unexpected exception', false, error instanceof Error ? `${error.name}: ${error.message}` : String(error))
}

for (const line of results) console.log(line)
console.log(failures === 0 ? `\nALL PASS (${results.length} checks)` : `\n${failures} FAILED of ${results.length}`)
process.exitCode = failures === 0 ? 0 : 1
