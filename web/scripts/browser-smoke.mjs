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
try {
  await waitForServer(baseUrl, fixture, () => fixtureOutput)
  browser = await chromium.launch({ headless: true })
  context = await browser.newContext()
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true })
  page = await context.newPage()
  page.setDefaultTimeout(15_000)

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '欢迎回来' }).waitFor({ state: 'visible' })
  await page.getByLabel('用户名').fill('browser-smoke')
  await page.getByLabel('密码').fill('browser-smoke-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL((url) => url.pathname.startsWith('/dashboard'), { timeout: 15_000 })
  await page.getByLabel('后台导航').waitFor({ state: 'visible' })

  // The route guard must restore the cookie-backed session after a full reload.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByLabel('后台导航').waitFor({ state: 'visible' })
  assert.match(new URL(page.url()).pathname, /^\/dashboard(?:\/|$)/)

  await page.goto(`${baseUrl}/dashboard/search/`, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('搜索关键词').fill('musicdl browser smoke')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await page.getByRole('heading', { name: 'Browser Smoke Track' }).waitFor({ state: 'visible' })
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
  await historyPage.screenshot({ path: successScreenshot, fullPage: true })
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

  await page.getByLabel('后台导航').getByRole('link', { name: '下载历史' }).click()
  const admission = page.getByRole('region', { name: '下载并发' })
  await admission.locator('dd').first().waitFor({ state: 'visible' })
  const countsBeforeFailure = await admission.innerText()
  assert.match(countsBeforeFailure, /\d+\s*\/\s*\d+/)

  // This is the only intercepted application request. Its first response and
  // every other API call above are served by the real fixture application.
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

  await context.tracing.stop()
  console.log(`SUCCESS: real browser smoke; completed download detail screenshot: ${successScreenshot}`)
} catch (error) {
  failure = error
  if (page) {
    try {
      await page.screenshot({ path: failureScreenshot, fullPage: true })
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
