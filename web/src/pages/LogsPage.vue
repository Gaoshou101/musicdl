<template>
  <div class="page-stack logs-page">
    <header class="page-heading-row logs-heading">
      <div><p class="eyebrow">可观测性</p><h1>日志查看</h1><p class="muted">服务只把日志留在内存里，重启即清空；下载事件与管理操作每 10 秒刷新，服务日志每 3 秒增量拉取。</p></div>
      <div class="log-actions">
        <label v-if="tab === 'service'" class="toolbar-label">日志级别
          <select :value="level" aria-label="日志级别" class="toolbar-select" @change="changeLevel(($event.target as HTMLSelectElement).value as LogLevel)">
            <option value="info">信息及以上</option><option value="warning">警告及以上</option><option value="error">错误及以上</option>
          </select>
        </label>
        <label v-else class="toolbar-label">过滤
          <select v-model="filter" aria-label="过滤条件" class="toolbar-select"><option value="all">全部</option><option value="failed">仅失败与异常</option></select>
        </label>
        <label class="check-line compact"><input v-model="autoScroll" type="checkbox" /><span>自动滚动</span></label>
        <button v-if="tab === 'service'" class="secondary-button" type="button" @click="paused = !paused">{{ paused ? '▶ 继续' : 'Ⅱ 暂停' }}</button>
        <button class="secondary-button" type="button" @click="refresh">↻ 刷新</button>
      </div>
    </header>
    <div class="log-tabs" role="tablist" aria-label="日志类型">
      <button v-for="item in tabs" :key="item.id" type="button" role="tab" :aria-selected="tab === item.id" :class="{ selected: tab === item.id }" @click="tab = item.id">
        {{ item.label }} <span>{{ item.id === 'events' ? totals.events : item.id === 'audit' ? totals.audit : logTotal }}</span>
      </button>
    </div>
    <p v-if="error" role="alert" class="notice notice-error">{{ error }}</p>
    <section class="panel log-panel">
      <p v-if="tab === 'service'" class="log-caption">这里显示的是服务进程自己写入的日志（内存中最多保留最近 500 行，重启即清空），不是容器 stdout 或 Docker 日志。<template v-if="dropped > 0">已有 {{ dropped }} 行更早的记录滚出窗口。</template><template v-if="paused">已暂停，点「继续」或「刷新」恢复。</template></p>
      <div ref="scrollRegion" class="log-list">
        <div v-if="empty" class="empty-state"><span>{{ tab === 'service' ? '≋' : '◌' }}</span><p>{{ tab === 'service' ? '暂无服务日志' : filter === 'all' ? '暂无日志' : '没有失败记录' }}</p></div>
        <template v-else-if="tab === 'events'"><article v-for="(event,index) in visibleEvents" :key="`${event.request_id}-${event.stage}-${index}`" class="log-row" :class="statusClass(event.status)">
          <div class="log-row-top"><strong>{{ stageText[event.stage ?? ''] ?? event.stage ?? '事件' }} · {{ statusText[event.status ?? ''] ?? event.status ?? '—' }}</strong><code>{{ event.source_id ?? '—' }}</code><code v-if="event.source_version">v{{ event.source_version }}</code><code v-if="event.error_code" class="tone-danger">{{ event.error_code }}</code></div>
          <div class="log-row-meta"><span>候选 {{ shortHash(event.candidate_id, 16) }}</span><span v-if="typeof event.size_bytes === 'number'">{{ formatBytes(event.size_bytes) }}</span><span v-if="event.healthy != null">音源健康 {{ event.healthy ? '是' : '否' }}</span><span v-if="event.sha256">sha256 {{ shortHash(event.sha256, 12) }}</span></div>
          <a v-if="event.relative_path" :href="mediaUrl(event.relative_path)" target="_blank" rel="noreferrer" class="log-path">{{ event.relative_path }}</a>
        </article></template>
        <template v-else-if="tab === 'audit'"><article v-for="(entry,index) in visibleAudit" :key="`${entry.action}-${index}`" class="log-row" :class="statusClass(typeof entry.status === 'string' ? entry.status : '')"><strong>{{ actionText[String(entry.action)] ?? entry.action ?? '操作' }} · {{ statusText[String(entry.status)] ?? entry.status ?? '—' }}</strong><p v-if="auditExtras(entry)" class="log-audit-extra">{{ auditExtras(entry) }}</p></article></template>
        <template v-else><article v-for="entry in logs" :key="entry.id" class="log-row service-row" :class="levelClass(entry.level)"><div class="log-row-top"><time>{{ clockTime(entry.time) }}</time><span>{{ levelText[entry.level.toLowerCase()] ?? entry.level.toLowerCase() }}</span><code>{{ entry.logger }}</code><p>{{ entry.message }}</p></div><pre v-if="entry.traceback">{{ entry.traceback }}</pre></article></template>
      </div>
      <footer v-if="tab !== 'service'" class="log-footer"><span>共 {{ tab === 'events' ? totals.events : totals.audit }} 条记录</span><button class="quiet-button" type="button" :disabled="offsets[tab] <= 0 || Boolean(olderFlights[tab])" @click="loadOlder">加载更早记录</button></footer>
    </section>
    <p v-if="loading" class="subtle">正在读取日志…</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowReactive, watch } from 'vue'
import { errorMessage, mediaUrl, readAudit, readEvents, readServiceLogs, type AuditEntry, type DownloadEvent, type Page, type ServiceLogEntry } from '@/lib/api'
import { formatBytes, shortHash } from '@/lib/format'
import { usePolling } from '@/composables/usePolling'
import { clockTime, EVENT_POLL_MS, mergeServiceLogs, readTail, SERVICE_POLL_MS, visibleEventRows } from '@/lib/logs'

type Tab = 'events' | 'audit' | 'service'
type Filter = 'all' | 'failed'
type LogLevel = 'info' | 'warning' | 'error'
const tabs: { id: Tab; label: string }[] = [{ id: 'events', label: '下载事件' }, { id: 'audit', label: '管理操作' }, { id: 'service', label: '服务日志' }]
const tab = ref<Tab>('events')
const filter = ref<Filter>('all')
const events = ref<DownloadEvent[]>([])
const audit = ref<AuditEntry[]>([])
const totals = ref({ events: 0, audit: 0 })
const offsets = ref<Record<'events' | 'audit', number>>({ events: 0, audit: 0 })
const loading = ref(true)
const error = ref('')
const autoScroll = ref(true)
const logs = ref<ServiceLogEntry[]>([])
const level = ref<LogLevel>('info')
const logTotal = ref(0)
const dropped = ref(0)
const paused = ref(false)
const scrollRegion = ref<HTMLElement | null>(null)
let lastId = 0
let generation: string | undefined
let serviceReset = true
const stageText: Record<string, string> = { download: '下载', cleanup: '清理', refresh: '重试刷新', health: '健康检查' }
const statusText: Record<string, string> = { success: '成功', failed: '失败', unavailable: '不可用', rate_limited: '被限流' }
const actionText: Record<string, string> = { login: '登录', change_credentials: '修改凭据', create_source: '导入音源', update_source: '修改音源', delete_source: '删除音源', create_bot: '添加 Bot', update_bot: '修改 Bot', delete_bot: '删除 Bot' }
const levelText: Record<string, string> = { debug: '调试', info: '信息', warning: '警告', error: '错误', critical: '严重' }
const visibleEvents = computed(() => visibleEventRows(events.value, filter.value))
const visibleAudit = computed(() => visibleEventRows(audit.value, filter.value))
const empty = computed(() => tab.value === 'events' ? !visibleEvents.value.length : tab.value === 'audit' ? !visibleAudit.value.length : !logs.value.length)
const eventsEnabled = computed(() => tab.value !== 'service')
const serviceEnabled = computed(() => tab.value === 'service' && !paused.value)
type PagedTab = 'events' | 'audit'
const olderFlights = shallowReactive<Record<PagedTab, Promise<void> | null>>({ events: null, audit: null })
const olderControllers: Record<PagedTab, AbortController | null> = { events: null, audit: null }
let mounted = true
let viewRevision = 0
let preserveScrollAnchor = false

function mergePagedRows<T>(current: T[], currentOffset: number, latest: Page<T>): { items: T[]; offset: number } {
  if (!current.length) return { items: latest.items, offset: latest.offset }
  const olderCount = latest.offset - currentOffset
  if (olderCount < 0) return { items: latest.items, offset: latest.offset }
  return { items: [...current.slice(0, olderCount), ...latest.items], offset: currentOffset }
}

function cancelOlderLoads(): void {
  for (const kind of ['events', 'audit'] as const) {
    olderControllers[kind]?.abort()
    olderControllers[kind] = null
    olderFlights[kind] = null
  }
}

async function fetchEvents(signal: AbortSignal) {
  const expectedRevision = viewRevision
  const expectedTab = tab.value
  const expectedFilter = filter.value
  try {
    const [eventPage, auditPage] = await Promise.all([readTail(readEvents, 100, signal), readTail(readAudit, 100, signal)])
    if (signal.aborted || !mounted || expectedRevision !== viewRevision || expectedTab !== tab.value || expectedFilter !== filter.value) return
    const mergedEvents = mergePagedRows(events.value, offsets.value.events, eventPage)
    const mergedAudit = mergePagedRows(audit.value, offsets.value.audit, auditPage)
    events.value = mergedEvents.items; audit.value = mergedAudit.items
    totals.value = { events: eventPage.total, audit: auditPage.total }
    offsets.value = { events: mergedEvents.offset, audit: mergedAudit.offset }
    error.value = ''
  } catch (cause) { if (!signal.aborted) error.value = errorMessage(cause) }
  finally { if (!signal.aborted) loading.value = false }
}
async function fetchService(signal: AbortSignal) {
  const reset = serviceReset
  const previousGeneration = generation
  try {
    const page = await readServiceLogs(200, reset ? 0 : lastId, level.value, signal, generation)
    if (signal.aborted) return
    serviceReset = false; lastId = page.last_id; generation = page.generation
    logTotal.value = page.total; dropped.value = page.dropped
    logs.value = mergeServiceLogs(logs.value, page, reset, previousGeneration)
    error.value = ''
  } catch (cause) { if (!signal.aborted) error.value = errorMessage(cause) }
  finally { if (!signal.aborted) loading.value = false }
}
const refreshEvents = usePolling(fetchEvents, EVENT_POLL_MS, eventsEnabled)
const refreshService = usePolling(fetchService, SERVICE_POLL_MS, serviceEnabled)
watch([tab, filter], ([nextTab], [previousTab]) => {
  viewRevision += 1
  cancelOlderLoads()
  if (nextTab !== 'service' && previousTab !== 'service') void refreshEvents(true)
})
watch([events, audit, logs], async () => {
  const region = scrollRegion.value
  if (region) {
    const previousHeight = region.scrollHeight
    const previousTop = region.scrollTop
    const keepAnchor = preserveScrollAnchor
    preserveScrollAnchor = false
    const nearBottom = autoScroll.value && previousHeight - region.clientHeight - previousTop <= 24
    await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)))
    if (!mounted || scrollRegion.value !== region) return
    if (keepAnchor) region.scrollTop = previousTop + (region.scrollHeight - previousHeight)
    else if (nearBottom) region.scrollTo?.({ top: region.scrollHeight, behavior: 'smooth' })
  }
})
function changeLevel(value: LogLevel) {
  if (value === level.value) return
  level.value = value; lastId = 0; generation = undefined; serviceReset = true; logs.value = []
  void refreshService(true)
}
function refresh() {
  if (tab.value !== 'service') { void refreshEvents(true); return }
  serviceReset = true
  if (paused.value) paused.value = false
  else void refreshService(true)
}
function loadOlder(): Promise<void> {
  const kind = tab.value
  if (kind === 'service') return Promise.resolve()
  if (olderFlights[kind]) return olderFlights[kind]!
  const nextOffset = Math.max(0, offsets.value[kind] - 100)
  if (nextOffset === offsets.value[kind]) return Promise.resolve()
  const requestRevision = viewRevision
  const requestTab = tab.value
  const requestFilter = filter.value
  const controller = new AbortController()
  olderControllers[kind] = controller
  let request!: Promise<void>
  request = (async () => {
    try {
      const page: Page<DownloadEvent | AuditEntry> = kind === 'events'
        ? await readEvents(nextOffset, 100, controller.signal)
        : await readAudit(nextOffset, 100, controller.signal)
      if (!mounted || controller.signal.aborted || requestRevision !== viewRevision || requestTab !== tab.value || requestFilter !== filter.value) return
      preserveScrollAnchor = true
      if (kind === 'events') events.value = [...page.items as DownloadEvent[], ...events.value]
      else audit.value = [...page.items as AuditEntry[], ...audit.value]
      offsets.value[kind] = page.offset
    } catch (cause) {
      if (mounted && !controller.signal.aborted && requestRevision === viewRevision) error.value = errorMessage(cause)
    } finally {
      if (olderFlights[kind] === request) olderFlights[kind] = null
      if (olderControllers[kind] === controller) olderControllers[kind] = null
    }
  })()
  olderFlights[kind] = request
  return request
}
function auditExtras(entry: AuditEntry) { return Object.entries(entry).filter(([key]) => !['action', 'status'].includes(key)).map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : String(value)}`).join('  ') }
function statusClass(status?: string) { return ({ success: 'log-success', failed: 'log-danger', unavailable: 'log-warning', rate_limited: 'log-warning' } as Record<string, string>)[status ?? ''] ?? '' }
function levelClass(value: string) { return value.toLowerCase() === 'error' || value.toLowerCase() === 'critical' ? 'log-danger' : value.toLowerCase() === 'warning' ? 'log-warning' : '' }
onBeforeUnmount(() => {
  mounted = false
  viewRevision += 1
  cancelOlderLoads()
})
</script>
