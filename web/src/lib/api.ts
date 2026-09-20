/**
 * The one client every page uses to reach the musicdl admin API.
 *
 * Three things the panel cannot get wrong live here rather than in seven
 * pages: the `/api/*` -> `/admin/*` hop, the double-submit CSRF header every
 * mutating call needs, and the translation of the backend's `detail`
 * envelopes into something an operator can read.
 *
 * The backend answers with English `detail` strings; a few of them are the
 * ones an operator will actually meet, so they are translated by name and
 * anything unrecognised is shown as it arrived rather than swallowed.
 */

/** Exactly the widenings a stored script carries, as the backend reports them. */
export type EgressPolicy = {
  allowed_hosts: string[]
  allowed_ports: number[]
  allow_insecure_http: boolean
  allow_ip_hosts: boolean
  allow_any_host: boolean
}

export type SourcePlugin = {
  sha256: string
  version: string
  language: string
  egress: EgressPolicy
}

export type SourceItem = {
  id: string
  enabled: boolean
  priority: number
  timeout: number
  name: string | null
  plugin: SourcePlugin | null
}

export type BotItem = {
  id: string
  enabled: boolean
  priority: number
  timeout: number
  username: string | null
  command_template: string | null
}

export type Candidate = {
  source_id: string
  source_version: string
  item_id: string
  title: string
  artist: string
  album?: string | null
  duration?: number | null
  bitrate?: number | null
  format?: string | null
  size?: number | null
  /**
   * Which catalogue a row came from, when the channel that listed it did not
   * answer out of an index of its own. Every installed lx source resolves
   * against the same four catalogues, so this is what tells a kuwo listing
   * apart from the netease one; `null` for a channel that answered itself.
   */
  platform?: string | null
}

export type SourceStatus = {
  id: string
  status: string
  count: number
  /** True when this channel answers from the one shared catalogue search. */
  catalogue?: boolean
}

/** How one channel has behaved lately, as `GET /sources/health` rolls it up. */
export type SourceHealthVerdict = 'ok' | 'degraded' | 'failing' | 'unknown'

export type SourceHealthRow = {
  id: string
  name: string | null
  enabled: boolean
  priority: number | null
  /** False for a channel that has traffic but is no longer configured. */
  configured: boolean
  status: SourceHealthVerdict
  attempts: number
  successes: number
  failures: number
  success_rate: number | null
  searches: number
  downloads: number
  refreshes: number
  last_search: string | null
  last_download: string | null
  last_refresh: string | null
  last_count: number
  last_error: string | null
  last_error_stage: string | null
  last_health: boolean | null
  last_health_status: string | null
}

export type SourceHealthReport = {
  sources: SourceHealthRow[]
  /** How many recent outcomes the rate is computed from. */
  window: number
  total: number
}

export type SearchReport = {
  query: string
  version: string
  count: number
  total: number
  candidates: Candidate[]
  sources: SourceStatus[]
  /**
   * Aligned with `candidates`: the channels behind each row, best first. A
   * candidate names the channel a download starts with; this names what a
   * retry may reach when that one cannot serve the bytes.
   */
  offers?: string[][]
}

export type DownloadReport = {
  request_id: string
  source_id: string
  /** The channel that failed first, when another one served the file. */
  fallback_from: string | null
  relative_path: string
  sha256: string
  size_bytes: number
  media_type: string
  extension: string
  language: string
}

export type CheckState = 'ok' | 'failed' | 'unavailable' | 'not_required'

export type HealthReport = {
  status: 'ok' | 'degraded'
  checks: {
    readyz: CheckState
    redis: CheckState
    plugin_runner: CheckState
    telegram: CheckState
  }
}

export type DownloadEvent = {
  request_id?: string
  candidate_id?: string
  source_id?: string
  source_version?: string
  stage?: string
  status?: string
  error_code?: string | null
  size_bytes?: number | null
  sha256?: string | null
  relative_path?: string | null
  healthy?: boolean | null
}

export type AuditEntry = {
  action?: string
  status?: string
  [key: string]: unknown
}

/** One line this service process logged, as `GET /logs` hands it back. */
export type ServiceLogEntry = {
  id: number
  /** ISO-8601 UTC; the page renders it in the viewer's own time zone. */
  time: string
  level: string
  logger: string
  message: string
  traceback: string | null
}

export type ServiceLogPage = {
  items: ServiceLogEntry[]
  total: number
  /** The cursor to pass back as `after`; unchanged when nothing new arrived. */
  last_id: number
  /** How many lines have rolled out of the window since the process started. */
  dropped: number
}

export type Page<T> = { items: T[]; total: number; offset: number; limit: number }

export type SourceImport = {
  id: string
  script: string
  filename?: string
  language?: 'javascript' | 'python'
  version?: string
  allow_insecure_http?: boolean
  allow_ip_hosts?: boolean
  allow_any_host?: boolean
  allowed_ports?: number[]
}

export type ImportPreview = {
  id: { value: string | null; valid: boolean; reason: string | null; suggested: string }
  install_path: 'lx' | 'generic'
  language: string | null
  operations: string[]
  required_grants: Record<string, unknown>
  granted: Record<string, unknown>
  missing_grants: string[]
  installable: boolean
  refusal: string | null
  analysis: Record<string, unknown>
}

/** How the panel should render one setting it is allowed to write. */
export type ConfigKind = 'bool' | 'int' | 'float' | 'text' | 'list' | 'secret'

/** `hot` is adopted by the running process; `restart` waits for the next start. */
export type ConfigScope = 'hot' | 'restart'

/** Where the value in force came from: the panel's layer, the deployment, or a default. */
export type ConfigSource = 'panel' | 'env' | 'default'

export type ConfigFieldView = {
  key: string
  /** The environment variable that sets the same thing from outside the process. */
  env: string
  label: string
  kind: ConfigKind
  scope: ConfigScope
  help: string
  secret: boolean
  source: ConfigSource
  /** The value in force; always `null` for a secret, which is never read back. */
  value: unknown
  set: boolean
}

export type ConfigGroupView = {
  id: string
  label: string
  help: string
  fields: ConfigFieldView[]
}

/** A setting only the deployment can change; the panel can name it, not move it. */
export type ContainerKnob = {
  key: string
  label: string
  env: string | null
  help: string
}

export type ConfigReport = {
  groups: ConfigGroupView[]
  container: ContainerKnob[]
  /** Stored values this build refused at startup, with the reason each was dropped. */
  rejected: Record<string, string>
}

/**
 * What a save cost the running process: the runtime it rebuilt in place, or
 * why it could not. A reply without one means nothing had to be rebuilt.
 */
export type ReloadReport = {
  status: 'reloaded' | 'failed' | 'skipped'
  reason?: string
  error?: string
  /** Which runtime the change produced; the first one is 1. */
  generation?: number
  sources?: number
  workers?: number
}

/** A mutation reply that may carry the rebuild it caused. */
export type MutationReport<T> = T & { reload?: ReloadReport }

export type ConfigWriteReport = ConfigReport & { reload?: ReloadReport }

/**
 * One clause about what a save cost the running process, for a notice line.
 *
 * Nothing to say is the common case: a mutation that changed nothing the
 * runtime reads carries no report at all, and the caller falls back to saying
 * the change is in force.
 */
export function reloadNote(report?: ReloadReport): string {
  if (!report) return ''
  if (report.status === 'reloaded') {
    return `，运行配置已热重载（第 ${report.generation ?? 1} 代，${report.sources ?? 0} 个音源）`
  }
  if (report.status === 'failed') {
    return `，但热重载失败：${report.error ?? '原因未知'}；改动已保存，重启后生效`
  }
  return ''
}

export type LoginReport = { ok: boolean; must_change: boolean; csrf_token: string }

const CSRF_HEADER = 'x-csrf-token'
const CSRF_COOKIE = 'csrf_token'
// A token the login reply handed us survives a reload in the tab it was read
// in. It is the same value the backend put in its own (non-httpOnly) cookie,
// so storing it here adds no trust the deployment did not already grant.
const CSRF_STORAGE_KEY = 'tgmusic.csrf-token'
export const USERNAME_STORAGE_KEY = 'tgmusic.username'

const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** The origin the panel is served from, or a test/standalone override. */
function configuredBase(): string {
  const value = typeof process !== 'undefined' ? process.env.NEXT_PUBLIC_API_BASE : undefined
  return value ? value.replace(/\/+$/, '') : ''
}

/** Messages the backend's fixed strings map onto, keyed by their exact text. */
const DETAIL_TEXT: Record<string, string> = {
  'invalid credentials': '用户名或密码不正确',
  'authentication required': '登录状态已失效，请重新登录',
  'credential change required': '请先修改默认密码',
  // Refreshing does not help: the token half of the pair only ever comes back
  // with a login reply, so a page that lost it (a second tab, a cleared
  // sessionStorage) has to sign in again rather than reload.
  'CSRF validation failed': '安全校验失败，请重新登录后重试',
  'too many login attempts': '登录尝试过于频繁，请稍后再试',
  'telegram connector is disabled': 'Telegram 连接器未启用：先在运行配置里打开并保存',
  'a phone number is required': '请填写手机号（含国家区号）',
  'the login code is required': '请填写 Telegram 发来的登录码',
  'the two-step password is required': '请填写两步验证密码',
  'plugin storage is unavailable': '插件存储不可用，无法安装或删除音源',
  'search runtime is unavailable': '搜索运行环境不可用，请确认音源已就绪',
  'media root is unavailable': '媒体目录不可用',
  'source not found': '音源不存在',
  'bot not found': 'Bot 不存在',
  'invalid candidate': '候选歌曲信息无效，请重新搜索',
  'source cannot resolve media': '该音源不支持解析这首歌的下载地址',
  'invalid priority': '优先级必须是 -1000 到 1000 之间的整数',
  'invalid timeout': '超时必须是 0.1 到 300 秒之间的数字',
  'invalid source configuration': '音源配置无效',
  'invalid id': 'ID 无效',
  'invalid request': '请求内容无效',
  'script is required': '请先选择音源脚本文件',
  'language must be javascript or python': '语言必须是 javascript 或 python',
  'invalid pagination': '分页参数无效',
  'invalid limit': '条数取值范围为 1-200',
  'invalid level': '日志级别无效',
  'invalid_bot_username': 'Bot 用户名无效：以字母开头，长度 5-32，只能包含字母、数字和下划线，且不含 @',
  'invalid_command_template': '命令模板必须包含且只能包含一个 {query} 占位符',
  'duplicate id': '该 ID 已存在',
  'too_many_bot_results': '搜索结果过多',
  'no configuration was provided': '没有需要保存的改动',
  // The media pipeline's own codes, which the download route returns verbatim.
  download_failed: '音源没能取到音频，可能该平台需要会员或链接已失效',
  empty_download: '音源返回了空文件',
  file_too_large: '音频文件超过体积上限',
  size_mismatch: '下载体积与音源声明的不一致',
  unsupported_extension: '音频扩展名不受支持',
  signature_mismatch: '文件内容与扩展名不符',
  extension_mismatch: '文件内容与扩展名不符',
  mime_mismatch: '返回的媒体类型与内容不符',
  path_escape: '目标路径越界，已拒绝写入',
  artifact_uncertain: '下载结果不完整，无法确认',
  media_url_denied: '音源给出的地址被出口策略拒绝',
  media_host_denied: '音源给出的域名不在允许列表内',
  media_dns_failed: '音频地址无法解析',
  media_address_denied: '音频地址指向的内网地址被拒绝',
  media_connect_failed: '连接音频服务器失败',
  media_tls_failed: '音频服务器 TLS 握手失败',
  media_timeout: '下载超时',
  media_redirect_denied: '跳转后的地址被出口策略拒绝',
  media_response_invalid: '音频服务器返回了无法识别的响应',
  source_unavailable: '音源当前不可用',
}

const DETAIL_PREFIX_TEXT: [string, string][] = [
  ['blocked lx source: ', '音源被安全策略拒绝：'],
  ['lx source needs an explicit operator grant: ', '该音源需要显式授权：'],
  ['an lx custom source is JavaScript', 'lx 自定义音源只能是 JavaScript'],
  ['allowed_hosts for an lx source must be the hosts the script declares', '允许的域名必须是脚本自己声明的域名'],
  ['unknown setting: ', '未知配置项：'],
]

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '发生未知错误'
}

/** The backend's one error envelope is `{"detail": ...}`; unpack it. */
function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string') {
    const trimmed = detail.trim()
    if (!trimmed) return null
    const known = DETAIL_TEXT[trimmed]
    if (known) return known
    for (const [prefix, replacement] of DETAIL_PREFIX_TEXT) {
      if (trimmed.startsWith(prefix)) return replacement + trimmed.slice(prefix.length)
    }
    return trimmed
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : null))
      .filter((item): item is string => Boolean(item))
    return parts.length ? parts.join('；') : null
  }
  if (detail && typeof detail === 'object' && 'msg' in detail) return String((detail as { msg: unknown }).msg)
  return null
}

function describeStatus(status: number): string {
  if (status === 401) return '登录状态已失效，请重新登录'
  if (status === 403) return '没有权限执行该操作'
  if (status === 404) return '请求的资源不存在'
  if (status === 409) return '该操作与当前状态冲突'
  if (status === 429) return '操作过于频繁，请稍后再试'
  if (status === 503) return '服务暂时不可用'
  if (status >= 500) return '服务端错误'
  return `请求失败（HTTP ${status}）`
}

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  for (const chunk of document.cookie.split(';')) {
    const separator = chunk.indexOf('=')
    if (separator < 0) continue
    if (chunk.slice(0, separator).trim() === name) return decodeURIComponent(chunk.slice(separator + 1).trim())
  }
  return null
}

let csrfToken: string | null = null

/**
 * Remember the token the backend just handed out.
 *
 * The backend also sets a `csrf_token` cookie, but it sets no `Path`, so a
 * browser scopes that cookie to the request path's directory -- `/api`, since
 * every call here goes through the panel's own `/api/*` hop. A page like
 * `/dashboard` therefore cannot read it back with `document.cookie`, while the
 * cookie itself still travels with every `/api/*` request, which is exactly
 * what the backend's double-submit check compares against. Keeping the value
 * from the login (and credential change) reply is what makes the header half
 * of that pair available to the page.
 */
function rememberCsrf(token: unknown): void {
  if (typeof token !== 'string' || !token) return
  csrfToken = token
  try {
    window.sessionStorage.setItem(CSRF_STORAGE_KEY, token)
  } catch {
    // A tab with storage disabled still works: the token stays in memory.
  }
}

function currentCsrf(): string | null {
  if (csrfToken) return csrfToken
  try {
    csrfToken = window.sessionStorage.getItem(CSRF_STORAGE_KEY)
  } catch {
    csrfToken = null
  }
  return csrfToken ?? readCookie(CSRF_COOKIE)
}

function forgetCsrf(): void {
  csrfToken = null
  try {
    window.sessionStorage.removeItem(CSRF_STORAGE_KEY)
  } catch {
    // Nothing to clear.
  }
}

/** A relative URL for one artifact the panel already downloaded. */
export function mediaUrl(relativePath: string): string {
  const path = relativePath.split('/').map(encodeURIComponent).join('/')
  return `${configuredBase()}/api/media/${path}`
}

type Query = Record<string, string | number | boolean | undefined | null>

type RequestOptions = {
  method?: string
  body?: unknown
  query?: Query
  /** Set for the calls a signed-out browser is allowed to make. */
  anonymous?: boolean
}

function withQuery(path: string, query?: Query): string {
  const base = `${configuredBase()}/api${path}`
  if (!query) return base
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `${base}?${encoded}` : base
}

async function readPayload(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('json')) return text
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/**
 * Send one request, and turn anything that went wrong into an ApiError.
 *
 * A 401 or a 403 that asks for a credential change sends the browser back to
 * the page that can fix it; every other failure stays with the caller, which
 * is the only place that knows what the operator was trying to do.
 */
async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers: Record<string, string> = { Accept: 'application/json' }
  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }
  if (MUTATING.has(method) && !options.anonymous) {
    const token = currentCsrf()
    if (token) headers[CSRF_HEADER] = token
  }

  let response: Response
  try {
    response = await fetch(withQuery(path, options.query), {
      method,
      headers,
      body,
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch {
    throw new ApiError(0, '无法连接到管理服务，请检查网络后重试')
  }

  const payload = await readPayload(response)
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' ? (payload as { detail?: unknown }).detail : null
    const error = new ApiError(response.status, describeDetail(detail) ?? describeStatus(response.status), detail)
    redirectIfSignedOut(response.status, error.message)
    throw error
  }
  return payload as T
}

function redirectIfSignedOut(status: number, message: string): void {
  if (typeof window === 'undefined') return
  if (window.location.pathname === '/') return
  if (status === 401) {
    forgetCsrf()
    window.location.replace('/')
    return
  }
  if (status === 403 && message === DETAIL_TEXT['credential change required']) {
    window.location.replace('/dashboard/settings?must_change=1')
  }
}

export async function login(username: string, password: string): Promise<LoginReport> {
  const report = await request<LoginReport>('/login', {
    method: 'POST',
    body: { username, password },
    anonymous: true,
  })
  rememberCsrf(report.csrf_token)
  return report
}

/** Change the administrator's own credentials; the reply carries a new token. */
export async function changeCredentials(input: {
  username: string
  password: string
  newPassword: string
}): Promise<void> {
  const report = await request<{ ok: boolean; csrf_token?: string }>('/change-credentials', {
    method: 'POST',
    body: { username: input.username, password: input.password, new_password: input.newPassword },
  })
  rememberCsrf(report.csrf_token)
}

export function listSources(): Promise<{ items: SourceItem[] }> {
  return request<{ items: SourceItem[] }>('/sources')
}

export function analyzeSource(source: SourceImport): Promise<ImportPreview> {
  return request<ImportPreview>('/sources/analyze', { method: 'POST', body: source })
}

export function installSource(source: SourceImport): Promise<MutationReport<SourceItem>> {
  return request<MutationReport<SourceItem>>('/sources', { method: 'POST', body: source })
}

export function updateSource(
  id: string,
  changes: { enabled?: boolean; priority?: number; timeout?: number; name?: string | null },
): Promise<MutationReport<SourceItem>> {
  return request<MutationReport<SourceItem>>(`/sources/${encodeURIComponent(id)}`,
                                             { method: 'PATCH', body: changes })
}

export function deleteSource(id: string): Promise<MutationReport<SourceItem & { uninstalled: string[] }>> {
  return request<MutationReport<SourceItem & { uninstalled: string[] }>>(
    `/sources/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function listBots(): Promise<{ items: BotItem[] }> {
  return request<{ items: BotItem[] }>('/bots')
}

export function createBot(bot: {
  id: string
  username: string
  command_template?: string
  enabled?: boolean
  priority?: number
  timeout?: number
}): Promise<MutationReport<BotItem>> {
  return request<MutationReport<BotItem>>('/bots', { method: 'POST', body: bot })
}

export function updateBot(
  id: string,
  changes: {
    enabled?: boolean
    priority?: number
    timeout?: number
    username?: string
    command_template?: string | null
  },
): Promise<MutationReport<BotItem>> {
  return request<MutationReport<BotItem>>(`/bots/${encodeURIComponent(id)}`,
                                          { method: 'PATCH', body: changes })
}

export function deleteBot(id: string): Promise<MutationReport<BotItem>> {
  return request<MutationReport<BotItem>>(`/bots/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function searchCandidates(query: string, limit = 50): Promise<SearchReport> {
  return request<SearchReport>('/search', { query: { q: query, limit } })
}

/**
 * Download the candidate the search just listed, into the service's media root.
 *
 * The query the operator searched for goes along, because the backend retries
 * a failed channel by refreshing that same query: the candidate's own title is
 * only a fallback for a caller that has no query to hand.
 */
export function downloadCandidate(candidate: Candidate, query?: string): Promise<DownloadReport> {
  return request<DownloadReport>('/download', {
    method: 'POST',
    body: query ? { candidate, query } : { candidate },
  })
}

export function readHealth(): Promise<HealthReport> {
  return request<HealthReport>('/health')
}

/** What each channel did last, from the panel's own searches and downloads. */
export function listSourceHealth(): Promise<SourceHealthReport> {
  return request<SourceHealthReport>('/sources/health')
}

/** Everything the panel owns, everything the deployment owns, and what is in force. */
export function listConfig(): Promise<ConfigReport> {
  return request<ConfigReport>('/config')
}

/**
 * Save one batch of settings.
 *
 * A value of `null` clears the panel's own override and lets the deployment's
 * value stand again; a blank secret keeps whatever is already stored, because
 * the panel can never render an existing one for the operator to confirm.
 */
export function updateConfig(values: Record<string, unknown>): Promise<ConfigWriteReport> {
  return request<ConfigWriteReport>('/config', { method: 'PATCH', body: { values } })
}

/**
 * Whether the stored Telegram session can talk to bots right now.
 *
 * `invalid_session` is the one an operator meets after filling in api_id and
 * api_hash: those wire the client, they do not authorise an account, so the
 * first login has to be finished here before any bot can answer.
 */
export type TelegramStatus =
  | 'code_required'
  | 'password_required'
  | 'ready'
  | 'invalid_session'
  | 'rate_limited'
  | 'error'

export type TelegramReport = {
  enabled: boolean
  profile: string
  /** Whether the running runtime assembled a connector at all. */
  available: boolean
  status: TelegramStatus | null
  retry_after: number | null
  error: string | null
  /** The masked number a half-finished login is waiting on, if there is one. */
  pending_phone: string | null
}

export function readTelegram(): Promise<TelegramReport> {
  return request<TelegramReport>('/telegram')
}

/** Ask Telegram to send a login code to this number. */
export function beginTelegramLogin(phone: string): Promise<TelegramReport> {
  return request<TelegramReport>('/telegram/login', { method: 'POST', body: { phone } })
}

export function verifyTelegramLogin(code: string): Promise<TelegramReport> {
  return request<TelegramReport>('/telegram/login/verify', { method: 'POST', body: { code } })
}

export function submitTelegramPassword(password: string): Promise<TelegramReport> {
  return request<TelegramReport>('/telegram/login/password', { method: 'POST', body: { password } })
}

/** Forget the stored session; the next login starts from a fresh code. */
export function logoutTelegram(): Promise<TelegramReport> {
  return request<TelegramReport>('/telegram/logout', { method: 'POST' })
}

export function readEvents(offset = 0, limit = 100): Promise<Page<DownloadEvent>> {
  return request<Page<DownloadEvent>>('/events', { query: { offset, limit } })
}

export function readAudit(offset = 0, limit = 100): Promise<Page<AuditEntry>> {
  return request<Page<AuditEntry>>('/audit', { query: { offset, limit } })
}

/**
 * The service's own log window: what the process printed, not what it stored.
 *
 * `after` is a cursor rather than a page offset, so a window that polls gets
 * only the lines it has not shown yet; `0` asks for the newest ones.
 */
export function readServiceLogs(
  limit = 200,
  after = 0,
  level: 'info' | 'warning' | 'error' = 'info',
): Promise<ServiceLogPage> {
  return request<ServiceLogPage>('/logs', { query: { limit, after, level } })
}
