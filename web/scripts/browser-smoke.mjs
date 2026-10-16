import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = path.resolve(webRoot, '..')
const panelRoot = process.env.MUSICDL_BROWSER_PANEL_ROOT
if (!panelRoot) throw new Error('MUSICDL_BROWSER_PANEL_ROOT must point to the panel exported from the CI image')

const artifactRoot = path.resolve(
  process.env.MUSICDL_BROWSER_ARTIFACTS ?? path.join(repoRoot, 'artifacts', 'browser-smoke'),
)
await mkdir(artifactRoot, { recursive: true })
const successScreenshot = path.join(artifactRoot, 'download-detail-success.png')
const failureScreenshot = path.join(artifactRoot, 'browser-smoke-failure.png')
const failureTrace = path.join(artifactRoot, 'browser-smoke-failure-trace.zip')

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      server.close((error) => error ? reject(error) : resolve(address.port))
    })
  })
}

async function waitForServer(url, child, getOutput) {
  const deadline = Date.now() + 45_000
  let lastError
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`fixture app exited with ${child.exitCode}\n${getOutput()}`)
    }
    try {
      const response = await fetch(`${url}/healthz`, { signal: AbortSignal.timeout(1500) })
      if (response.ok) return
      lastError = new Error(`fixture health returned HTTP ${response.status}`)
    } catch (error) {
      lastError = error
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`fixture app did not become healthy: ${lastError}\n${getOutput()}`)
}

function stopChild(child) {
  if (!child || child.exitCode !== null) return Promise.resolve()
  return new Promise((resolve) => {
    const timeout = setTimeout(() => child.kill('SIGKILL'), 3000)
    child.once('exit', () => {
      clearTimeout(timeout)
      resolve()
    })
    child.kill('SIGTERM')
  })
}

const port = await freePort()
const baseUrl = `http://127.0.0.1:${port}`
let fixtureOutput = ''
const fixture = spawn(
  process.env.PYTHON ?? process.env.PYTHON_EXECUTABLE ?? 'python',
  ['tests/browser/fixture_app.py'],
  {
    cwd: repoRoot,
    env: {
      ...process.env,
      MUSICDL_BROWSER_PANEL_ROOT: path.resolve(panelRoot),
      MUSICDL_BROWSER_PORT: String(port),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  },
)
const keepOutput = (chunk) => {
  fixtureOutput = (fixtureOutput + chunk.toString()).slice(-12_000)
}
fixture.stdout.on('data', keepOutput)
fixture.stderr.on('data', keepOutput)

let browser
let context
let page
let failure
const browserErrors = []
try {
  await waitForServer(baseUrl, fixture, () => fixtureOutput)
  browser = await chromium.launch({ headless: true })
  context = await browser.newContext()
  await context.addInitScript(() => {
    // A previous release's persisted preference must survive the Vue upgrade.
    if (!localStorage.getItem('tgmusic.theme')) {
      localStorage.setItem('tgmusic.theme', JSON.stringify({
        state: { theme: 'emerald', appearance: 'light' }, version: 0,
      }))
    }
  })
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true })
  page = await context.newPage()
  page.on('pageerror', (error) => browserErrors.push(error.message))
  page.setDefaultTimeout(15_000)

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '欢迎回来' }).waitFor({ state: 'visible' })
  await page.screenshot({ path: path.join(artifactRoot, 'vue-login.png'), fullPage: true, animations: 'disabled' })
  await page.getByLabel('用户名').fill('browser-smoke')
  await page.getByLabel('密码').fill('browser-smoke-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL((url) => url.pathname.startsWith('/dashboard'), { timeout: 15_000 })
  await page.getByRole('navigation', { name: '后台导航', exact: true }).waitFor({ state: 'visible' })
  assert.equal(await page.locator('html').getAttribute('data-appearance'), 'light')
  assert.equal(await page.getByLabel('主题色').inputValue(), 'emerald')
  await page.getByLabel('主题色').selectOption('rose')
  await page.getByRole('button', { name: '切换深色模式', exact: true }).click()

  // The route guard must restore the cookie-backed session after a full reload.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByRole('navigation', { name: '后台导航', exact: true }).waitFor({ state: 'visible' })
  assert.match(new URL(page.url()).pathname, /^\/dashboard(?:\/|$)/)
  assert.equal(await page.locator('html').getAttribute('data-appearance'), 'dark')
  assert.equal(await page.getByLabel('主题色').inputValue(), 'rose')
  await page.getByRole('heading', { name: '依赖检查', exact: true }).waitFor({ state: 'visible' })
  await page.screenshot({ path: path.join(artifactRoot, 'vue-overview.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '切换浅色模式', exact: true }).click()
  await page.screenshot({ path: path.join(artifactRoot, 'vue-overview-light.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForFunction(() => {
    const sidebar = document.querySelector('.sidebar')
    return sidebar && sidebar.getBoundingClientRect().right <= 0
  })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
    'mobile overview must not scroll horizontally')
  await page.screenshot({ path: path.join(artifactRoot, 'vue-mobile-overview.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.getByRole('button', { name: '切换深色模式', exact: true }).click()

  await page.goto(`${baseUrl}/dashboard/search/`, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('搜索关键词').waitFor({ state: 'visible' })
  await page.screenshot({ path: path.join(artifactRoot, 'vue-search-empty.png'), fullPage: true, animations: 'disabled' })
  await page.getByLabel('搜索关键词').fill('musicdl browser smoke')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await page.getByRole('heading', { name: 'Browser Smoke Track' }).waitFor({ state: 'visible' })
  await page.screenshot({ path: path.join(artifactRoot, 'vue-search-results.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '切换浅色模式', exact: true }).click()
  await page.screenshot({ path: path.join(artifactRoot, 'vue-search-results-light.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForFunction(() => {
    const sidebar = document.querySelector('.sidebar')
    return sidebar && sidebar.getBoundingClientRect().right <= 0
  })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
    'mobile search results must not scroll horizontally')
  await page.screenshot({ path: path.join(artifactRoot, 'vue-mobile-search.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.getByRole('button', { name: '切换深色模式', exact: true }).click()
  await page.getByRole('button', { name: '下载到服务端媒体目录' }).click()

  const resolveStarted = await fetch(`${baseUrl}/__browser_fixture__/wait-for-resolve`, {
    signal: AbortSignal.timeout(35_000),
  })
  assert.equal(resolveStarted.status, 200, 'fixture source did not enter its held resolve span')

  const historyPage = await context.newPage()
  historyPage.setDefaultTimeout(15_000)
  await historyPage.goto(`${baseUrl}/dashboard/downloads/`, { waitUntil: 'domcontentloaded' })
  await historyPage.getByRole('button', { name: '查看 Browser Smoke Track 的下载记录' }).click()
  const details = historyPage.getByRole('region', { name: '下载详情' })
  await details.getByRole('heading', { name: 'Browser Smoke Track' }).waitFor({ state: 'visible' })
  await details.getByText('下载中', { exact: true }).waitFor({ state: 'visible' })
  await details.getByText('最近阶段：正在解析音源', { exact: true }).waitFor({ state: 'visible' })

  const released = await fetch(`${baseUrl}/__browser_fixture__/release-resolve`, { method: 'POST' })
  assert.equal(released.status, 200, 'fixture resolve release control did not respond')
  const transferStarted = await fetch(`${baseUrl}/__browser_fixture__/wait-for-transfer`, {
    signal: AbortSignal.timeout(35_000),
  })
  assert.equal(transferStarted.status, 200, 'fixture source did not enter its held transfer span')
  await details.getByRole('button', { name: '刷新下载详情' }).click()
  await details.getByText('最近阶段：正在接收并校验音频', { exact: true }).waitFor({ state: 'visible' })

  const transferReleased = await fetch(`${baseUrl}/__browser_fixture__/release-transfer`, { method: 'POST' })
  assert.equal(transferReleased.status, 200, 'fixture transfer release control did not respond')
  await page.getByText(/下载成功：/).waitFor({ state: 'visible' })
  await details.getByRole('button', { name: '刷新下载详情' }).click()
  await details.getByText('已完成', { exact: true }).waitFor({ state: 'visible' })
  await details.getByText('最近阶段：正在接收并校验音频', { exact: true }).waitFor({ state: 'detached' })
  await historyPage.getByRole('button', { name: '刷新列表', exact: true }).click()
  await historyPage.getByRole('region', { name: '下载记录' })
    .getByRole('button', { name: '查看 Browser Smoke Track 的下载记录', exact: true })
    .getByText('已完成', { exact: true }).waitFor({ state: 'visible' })
  await historyPage.getByRole('region', { name: '下载并发' }).locator('dd').first()
    .filter({ hasText: /^0\s*\/\s*[1-9]\d*$/ }).waitFor({ state: 'visible' })
  await historyPage.screenshot({ path: successScreenshot, fullPage: true, animations: 'disabled' })
  await historyPage.close()

  await page.goto(`${baseUrl}/dashboard/sources/`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '音源管理', exact: true }).waitFor({ state: 'visible' })
  await page.getByRole('button', { name: '导入音源', exact: true }).click()
  await page.getByRole('heading', { name: '导入音源脚本', exact: true }).waitFor({ state: 'visible' })
  await page.getByPlaceholder('https://example.com/source.js\nhttps://example.org/source.py')
    .fill('https://catalog.example/browser/catalog.json')
  await page.getByRole('button', { name: '获取并预览', exact: true }).click()
  const importQueue = page.getByRole('region', { name: '批量导入队列' })
  await importQueue.getByText('可安装 · 通用脚本', { exact: true }).waitFor({ state: 'visible' })
  const sourceId = await importQueue.getByLabel('音源 ID').inputValue()
  assert.match(sourceId, /^[a-z][a-z0-9-]{1,63}$/)
  await page.getByRole('button', { name: '安装可用音源', exact: true }).last().click()
  await importQueue.getByText('已安装', { exact: true }).waitFor({ state: 'visible' })
  await page.getByRole('heading', { name: sourceId, exact: true, level: 3 }).waitFor({ state: 'visible' })
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByRole('heading', { name: '导入音源脚本', exact: true }).waitFor({ state: 'detached' })

  // Exercise the remaining pages through real session-protected service routes.
  await page.goto(`${baseUrl}/dashboard/health`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '健康监控', exact: true }).waitFor()
  await page.goto(`${baseUrl}/dashboard/logs`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '日志查看', exact: true }).waitFor()
  await page.getByRole('tab', { name: /^服务日志/ }).click()
  await page.getByRole('tab', { name: /^管理操作/ }).click()

  await page.goto(`${baseUrl}/dashboard/config`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '运行配置', exact: true }).waitFor()
  const configField = page.getByLabel(/窗口内失败次数上限/)
  const originalLimit = await configField.inputValue()
  await configField.fill('6')
  const savedConfig = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/admin/config' && response.request().method() === 'PATCH',
  )
  await page.getByRole('button', { name: /保存改动/ }).click()
  assert.equal((await savedConfig).status(), 200)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByLabel(/窗口内失败次数上限/).waitFor()
  assert.equal(await page.getByLabel(/窗口内失败次数上限/).inputValue(), '6')
  await page.getByLabel(/窗口内失败次数上限/).fill(originalLimit)
  const restoredConfig = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/admin/config' && response.request().method() === 'PATCH',
  )
  await page.getByRole('button', { name: /保存改动/ }).click()
  assert.equal((await restoredConfig).status(), 200)

  // Only Telegram is mocked here: never send a real login code from CI.
  let telegramStatus = 'invalid_session'
  const telegramCalls = []
  const telegramReport = () => ({
    enabled: true, available: true, profile: 'browser-fixture', status: telegramStatus,
    retry_after: null, error: null,
    pending_phone: telegramStatus === 'code_required' ? '+86******8000' : null,
  })
  await page.route('**/admin/telegram{,/**}', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (method === 'POST') {
      assert.ok(route.request().headers()['x-csrf-token'], 'Telegram mutation omitted CSRF')
      telegramCalls.push({ pathname, body: route.request().postDataJSON() })
      telegramStatus = pathname.endsWith('/verify') ? 'password_required'
        : pathname.endsWith('/password') ? 'ready'
        : pathname.endsWith('/logout') ? 'invalid_session' : 'code_required'
    }
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(telegramReport()) })
  })
  await page.goto(`${baseUrl}/dashboard/bots`, { waitUntil: 'domcontentloaded' })
  const telegram = page.getByRole('region', { name: 'Telegram 账号会话' })
  await telegram.getByLabel(/手机号/).fill('+8613800138000')
  await telegram.getByRole('button', { name: '发送登录码', exact: true }).click()
  await telegram.getByLabel('Telegram 发来的登录码').fill('12345')
  await telegram.getByRole('button', { name: '验证并登录', exact: true }).click()
  await telegram.getByLabel('两步验证密码').fill('fixture-two-step-password')
  await telegram.getByRole('button', { name: '提交密码', exact: true }).click()
  await telegram.getByText('已登录', { exact: true }).waitFor()
  await telegram.getByRole('button', { name: '退出登录', exact: true }).click()
  await telegram.getByLabel(/手机号/).waitFor()
  assert.deepEqual(telegramCalls.map((call) => call.pathname), [
    '/admin/telegram/login', '/admin/telegram/login/verify',
    '/admin/telegram/login/password', '/admin/telegram/logout',
  ])
  assert.equal(telegramCalls[0].body.phone, '+8613800138000')
  assert.equal(telegramCalls[1].body.code, '12345')
  assert.equal(telegramCalls[2].body.password, 'fixture-two-step-password')

  await page.goto(`${baseUrl}/dashboard/settings`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '修改用户名与密码', exact: true }).waitFor()
  const restricted = await context.newPage()
  await restricted.route('**/admin/session', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ authenticated: true, must_change: true, user_id: 'browser-smoke' }),
  }))
  await restricted.goto(`${baseUrl}/dashboard/logs`, { waitUntil: 'domcontentloaded' })
  await restricted.waitForURL((url) => url.pathname === '/dashboard/settings' && url.searchParams.get('must_change') === '1')
  await restricted.getByText('需要修改默认密码', { exact: true }).waitFor()
  await restricted.close()

  // Rotate credentials in the disposable real app and verify old cookies stop
  // authenticating. The current browser must keep the replacement session.
  const oldSession = await browser.newContext({ storageState: await context.storageState() })
  try {
    await page.getByLabel('用户名', { exact: true }).fill('browser-smoke-updated')
    await page.locator('input[autocomplete="current-password"]').fill('browser-smoke-password')
    const newPasswords = page.locator('input[autocomplete="new-password"]')
    await newPasswords.nth(0).fill('browser-smoke-updated-password')
    await newPasswords.nth(1).fill('browser-smoke-updated-password')
    const rotated = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/admin/change-credentials' && response.request().method() === 'POST',
    )
    await page.getByRole('button', { name: '保存修改', exact: true }).click()
    assert.equal((await rotated).status(), 200)
    assert.equal((await oldSession.request.get(`${baseUrl}/admin/sources`)).status(), 401)
    assert.equal((await context.request.get(`${baseUrl}/admin/sources`)).status(), 200)
  } finally {
    await oldSession.close()
  }
  await page.setViewportSize({ width: 390, height: 844 })
  const menu = page.getByRole('button', { name: '打开后台导航', exact: true })
  await menu.click()
  await page.getByLabel('后台导航', { exact: true }).getByRole('link', { name: '下载历史' }).click()
  await page.getByRole('region', { name: '下载记录' }).waitFor()
  assert.equal(await menu.getAttribute('aria-expanded'), 'false')
  await page.waitForFunction(() => {
    const sidebar = document.querySelector('.sidebar')
    return sidebar && sidebar.getBoundingClientRect().right <= 0
  })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
    'mobile page must not scroll horizontally')
  await page.screenshot({ path: path.join(artifactRoot, 'vue-mobile-downloads.png'), fullPage: true, animations: 'disabled' })
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.getByLabel('后台导航', { exact: true }).getByRole('link', { name: '下载历史' }).click()
  const admission = page.getByRole('region', { name: '下载并发' })
  await admission.locator('dd').first().waitFor({ state: 'visible' })
  const countsBeforeFailure = await admission.innerText()
  assert.match(countsBeforeFailure, /\d+\s*\/\s*\d+/)

  // Fault injection for observability follows its real initial response.
  // Telegram and the forced-change state above use explicit browser fixtures;
  // login, credential rotation, config, search and downloads use the real app.
  await page.route('**/admin/downloads/observability', (route) => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ detail: '服务暂时不可用' }),
  }))
  const failedRefresh = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/admin/downloads/observability' && response.status() === 503,
  )
  await admission.getByRole('button', { name: '刷新下载并发' }).click()
  await failedRefresh
  await admission.getByRole('alert').getByText('服务暂时不可用', { exact: true })
    .waitFor({ state: 'visible' })
  assert.doesNotMatch(await admission.innerText(), /\d+\s*\/\s*\d+/)

  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await page.getByRole('heading', { name: '欢迎回来', exact: true }).waitFor()
  await page.goto(`${baseUrl}/dashboard/settings`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '欢迎回来', exact: true }).waitFor()
  assert.deepEqual(browserErrors, [], 'Vue pages raised unhandled browser errors')

  await context.tracing.stop()
  console.log(`SUCCESS: real browser smoke; completed download detail screenshot: ${successScreenshot}`)
} catch (error) {
  failure = error
  if (page) {
    try {
      await page.screenshot({ path: failureScreenshot, fullPage: true, animations: 'disabled' })
    } catch {
      // Preserve the original failure if the browser has already gone away.
    }
  }
  if (context) {
    try {
      await context.tracing.stop({ path: failureTrace })
    } catch {
      // A trace can already be stopped if a browser teardown failed.
    }
  }
  console.error(`Browser smoke failed. Fixture output:\n${fixtureOutput}`)
  console.error(`Failure screenshot: ${failureScreenshot}`)
  console.error(`Failure trace: ${failureTrace}`)
} finally {
  await context?.close().catch(() => {})
  await browser?.close().catch(() => {})
  await stopChild(fixture)
}

if (failure) {
  throw failure
}
