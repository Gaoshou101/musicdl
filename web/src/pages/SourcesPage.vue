<template>
  <section class="mx-auto w-full max-w-7xl space-y-6 px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
    <header class="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p class="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-accent-300">Source registry</p>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">音源管理</h1>
        <p class="mt-1.5 max-w-2xl text-sm leading-relaxed text-neutral-400">管理搜索与下载使用的音源脚本、出口权限和近期诊断结果。</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="rounded-full border border-neutral-800 bg-neutral-900/35 px-3 py-2 text-xs text-neutral-400">{{ enabledCount }} / {{ sources.length }} 已启用</span>
        <button type="button" class="action-secondary" :disabled="loading" aria-label="刷新音源列表" @click="loadSources"><ArrowClockwise :size="16" />刷新</button>
        <button type="button" class="action-primary" @click="showImport = true"><Plus :size="18" weight="bold" />导入音源</button>
      </div>
    </header>

    <p v-if="notice" role="status" class="rounded-xl border px-4 py-3 text-sm" :class="notice.tone === 'ok' ? 'border-success/20 bg-success/10 text-success' : 'border-danger/20 bg-danger/10 text-danger'">{{ notice.text }}</p>

    <section aria-label="音源诊断" class="glass rounded-2xl p-4 sm:p-5">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div class="max-w-2xl"><p class="text-xs font-semibold uppercase tracking-[0.14em] text-accent-300">Health check</p><h2 class="mt-1 text-lg font-semibold">诊断音源</h2>
          <p class="mt-1.5 text-sm leading-relaxed text-neutral-400">仅运行有界搜索与元数据解析，不下载音频；每项最多尝试 3 个查询。音源版本改变后旧结果会标记为过期。</p></div>
        <div class="grid gap-2 sm:grid-cols-[minmax(12rem,1fr)_auto]">
          <label class="sr-only" for="diagnostic-query">诊断查询</label>
          <input id="diagnostic-query" v-model="diagnosticQuery" aria-label="诊断查询" maxlength="500" placeholder="默认：周杰伦 晴天" class="field-control w-full text-sm">
          <button type="button" class="action-primary justify-center" :disabled="diagnosticBusy || !sources.some((source) => source.enabled)" @click="checkAll">{{ diagnosticBusy ? '提交中…' : '诊断全部启用音源' }}</button>
        </div>
      </div>
      <p v-if="diagnostics" class="mt-3 text-xs text-neutral-500">并发 {{ diagnostics.active }}/{{ diagnostics.active_limit }} · 等待 {{ diagnostics.pending }}/{{ diagnostics.pending_limit }}<span v-if="activeJobCount"> · {{ activeJobCount }} 项正在进行</span></p>
    </section>

    <p v-if="loading" role="status" class="py-8 text-center text-sm text-neutral-400">正在读取音源…</p>
    <section v-else aria-label="已注册的音源" class="space-y-3">
      <article v-for="source in sources" :key="source.id" class="glass rounded-2xl p-4 sm:p-5">
        <form v-if="editing === source.id" class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem_8rem_auto_auto] sm:items-end" @submit.prevent="saveEditing(source)">
          <label class="text-xs text-neutral-400">显示名称
            <input v-model="form.name" :placeholder="source.id" :aria-label="'显示名称 ' + source.id" class="field-control mt-1 w-full text-sm">
          </label>
          <label class="text-xs text-neutral-400">优先级
            <input v-model="form.priority" type="number" min="-1000" max="1000" :aria-label="'优先级 ' + source.id" class="field-control mt-1 w-full text-sm tabular-nums">
          </label>
          <label class="text-xs text-neutral-400">超时（秒）
            <input v-model="form.timeout" type="number" min="0.1" max="300" step="0.1" :aria-label="'超时 ' + source.id" class="field-control mt-1 w-full text-sm tabular-nums">
          </label>
          <button type="submit" class="action-primary justify-center"><Check :size="17" weight="bold" />保存</button>
          <button type="button" class="action-secondary justify-center" @click="editing = null"><X :size="17" />取消</button>
        </form>
        <template v-else>
          <div class="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div class="flex min-w-0 flex-1 gap-3 sm:gap-4">
              <div class="grid size-11 shrink-0 place-items-center rounded-xl bg-accent-500/10 text-accent-300"><MusicNote :size="22" weight="duotone" /></div>
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <h3 class="break-words font-semibold">{{ source.name || source.id }}</h3>
                  <span class="rounded-md bg-neutral-800 px-2 py-1 font-mono text-[11px] text-neutral-400">{{ source.id }}</span>
                  <span class="rounded-md px-2 py-1 text-[11px]" :class="source.enabled ? 'bg-success/15 text-success' : 'bg-neutral-800 text-neutral-400'">{{ source.enabled ? '已启用' : '已停用' }}</span>
                  <span v-if="channels[source.id]" class="rounded-md px-2 py-1 text-[11px]" :class="VERDICT_CLASS[channels[source.id]!.status]" title="搜索、刷新与下载的综合状态">综合 {{ VERDICT_TEXT[channels[source.id]!.status] }}</span>
                  <span v-if="channels[source.id]" class="rounded-md px-2 py-1 text-[11px]" :class="losslessClass(channels[source.id]!)" :title="losslessTitle(channels[source.id]!)">{{ losslessLabel(channels[source.id]!) }}<template v-if="channels[source.id]!.lossless_stale && channels[source.id]!.lossless_status !== 'unknown'">（已过期）</template></span>
                  <span v-if="!source.plugin" class="rounded-md bg-warning/15 px-2 py-1 text-[11px] text-warning">脚本未安装</span>
                </div>
                <p class="mt-1.5 text-sm text-neutral-400">优先级 {{ source.priority }} · 超时 {{ source.timeout }}s<template v-if="source.plugin"> · 版本 {{ source.plugin.version }} · {{ source.plugin.language }}</template></p>
                <p class="mt-1 break-words text-xs text-neutral-500">{{ egressText(source) }}</p>
                <ChannelMetrics v-if="channels[source.id]" :row="channels[source.id]" />
                <div v-if="activeBySource[source.id]" class="mt-3 rounded-lg border border-accent-500/20 bg-accent-500/5 px-3 py-2 text-xs text-neutral-300">
                  <div class="flex flex-wrap items-center justify-between gap-2"><span>{{ activeBySource[source.id]!.status === 'queued' ? '等待检查' : '正在检查：' + activeBySource[source.id]!.stage }} · 已等待 {{ waited(activeBySource[source.id]!) }}s</span><button type="button" class="text-neutral-400 hover:text-neutral-100" :aria-label="'取消诊断 ' + source.id" @click="cancelDiagnostic(activeBySource[source.id]!)">取消</button></div>
                  <p class="mt-1 truncate font-mono text-neutral-500" :title="activeBySource[source.id]!.query">查询：{{ activeBySource[source.id]!.query }}</p>
                </div>
                <div v-else-if="latestBySource[source.id]" class="mt-3 rounded-lg border border-neutral-800 bg-neutral-950/40 px-3 py-2 text-xs text-neutral-400">
                  <div class="flex flex-wrap items-center gap-x-3 gap-y-1"><span :class="latestBySource[source.id]!.stale ? 'text-warning' : latestBySource[source.id]!.status === 'ok' ? 'text-success' : 'text-neutral-300'">{{ latestBySource[source.id]!.stale ? '结果已过期 · ' : '' }}{{ DIAGNOSTIC_TEXT[latestBySource[source.id]!.status] ?? latestBySource[source.id]!.message }}</span><span>搜索 {{ ms(latestBySource[source.id]!.search_ms) }}</span><span>解析 {{ ms(latestBySource[source.id]!.resolve_ms) }}</span><span v-if="latestBySource[source.id]!.extension">{{ latestBySource[source.id]!.extension?.toUpperCase() }}<template v-if="latestBySource[source.id]!.quality"> · {{ latestBySource[source.id]!.quality }}</template></span><time>{{ dateTime(latestBySource[source.id]!.tested_at * 1000) }}</time></div>
                  <p class="mt-1 truncate font-mono text-neutral-500" :title="latestBySource[source.id]!.actual_query">实际查询：{{ latestBySource[source.id]!.actual_query }}</p>
                  <p class="mt-1 text-neutral-600">仅完成搜索与元数据解析，没有下载文件。</p>
                </div>
              </div>
            </div>
            <div class="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:w-auto xl:grid-cols-5">
              <button type="button" class="action-secondary justify-center px-2 text-xs" :disabled="!source.enabled || diagnosticBusy || Boolean(activeBySource[source.id])" :title="'执行有界搜索和元数据解析，不下载文件'" :aria-label="'诊断 ' + (source.name || source.id)" @click="checkOne(source)">诊断</button>
              <button type="button" class="action-secondary justify-center px-2 text-xs" :title="source.enabled ? '停用该音源' : '启用该音源'" :aria-label="source.enabled ? '停用该音源' : '启用该音源'" @click="toggleEnabled(source)"><Power :size="16" />{{ source.enabled ? '停用' : '启用' }}</button>
              <button type="button" class="action-secondary justify-center px-2 text-xs" :disabled="checking[source.id]" title="实测该渠道能否提供无损音质" aria-label="实测该渠道的无损能力" @click="checkLossless(source)"><SpinnerGap v-if="checking[source.id]" :size="16" class="animate-spin" /><Waveform v-else :size="16" />无损检查</button>
              <button type="button" class="action-secondary justify-center px-2 text-xs" title="编辑名称、优先级与超时" aria-label="编辑名称、优先级与超时" @click="startEditing(source)"><PencilSimple :size="16" />编辑</button>
              <button type="button" class="action-danger justify-center px-2 text-xs" title="删除该音源" aria-label="删除该音源" @click="removeSource(source)"><Trash :size="16" />删除</button>
            </div>
          </div>
        </template>
      </article>

      <div v-if="sources.length === 0" class="glass rounded-2xl px-6 py-12 text-center">
        <FileCode :size="40" weight="duotone" class="mx-auto text-neutral-500" />
        <h2 class="mt-3 font-semibold">暂无音源</h2><p class="mt-1 text-sm text-neutral-400">导入 lx-music 格式的 JS 脚本即可开始搜索与下载。</p>
        <button type="button" class="action-primary mt-5" @click="showImport = true">导入第一个音源</button>
      </div>
    </section>

    <SourceImportDialog v-if="showImport" :existing-ids="existingIds" @close="showImport = false" @changed="loadSources" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { PhArrowClockwise as ArrowClockwise, PhCheck as Check, PhFileCode as FileCode, PhMusicNote as MusicNote, PhPencilSimple as PencilSimple, PhPlus as Plus, PhPower as Power, PhSpinnerGap as SpinnerGap, PhTrash as Trash, PhWaveform as Waveform, PhX as X } from '@phosphor-icons/vue'
import {
  cancelSourceDiagnosticJob, checkSourceLossless, deleteSource, errorMessage, listSourceHealth,
  listSources, reloadNote, sourceDiagnostics, startSourceDiagnostic, startSourceDiagnosticBatch,
  updateSource,
  type LosslessStatus, type SourceDiagnosticJob, type SourceDiagnosticResult, type SourceDiagnosticSnapshot,
  type SourceHealthReport, type SourceHealthRow, type SourceItem,
} from '@/lib/api'
import ChannelMetrics from '@/components/ChannelMetrics.vue'
import SourceImportDialog from '@/features/sources/SourceImportDialog.vue'
import { currentDiagnosticSnapshotResults } from '@/features/sources/diagnosticSnapshot'
import { usePolling } from '@/composables/usePolling'

type Notice = { tone: 'ok' | 'error'; text: string } | null
const VERDICT_TEXT: Record<string, string> = { ok: '正常', degraded: '不稳定', failing: '异常', unknown: '未验证' }
const VERDICT_CLASS: Record<string, string> = { ok: 'bg-success/15 text-success', degraded: 'bg-warning/15 text-warning', failing: 'bg-danger/15 text-danger', unknown: 'bg-neutral-800 text-neutral-500' }
const LOSSLESS_TEXT: Record<LosslessStatus, string> = { lossless: '无损可用', lossy: '不可用', unknown: '未检测' }
const LOSSLESS_CLASS: Record<LosslessStatus, string> = { lossless: 'bg-success/15 text-success', lossy: 'bg-warning/15 text-warning', unknown: 'bg-neutral-800 text-neutral-500' }
const DIAGNOSTIC_TEXT: Record<string, string> = {
  ok: '搜索与 FLAC 解析响应正常', unsupported_flac: '可搜索，但未能提供 FLAC', empty_search: '搜索没有结果', search_failed: '搜索阶段失败',
  resolve_failed: '解析阶段失败', resolve_unknown: '解析有响应，但无法确认音质', resolve_unavailable: '未能安全执行解析', search_only: '只检查了搜索；未下载文件',
  timeout: '检查超时', auth_required: '上游需要授权', rate_limited: '上游正在限流', busy: '检查容量已满', cancelled: '检查已取消', source_unavailable: '当前运行时没有这个音源',
}
const DIAGNOSTIC_SEEN_LIMIT = 512
const sources = ref<SourceItem[]>([])
const channels = ref<Record<string, SourceHealthRow>>({})
const diagnostics = ref<SourceDiagnosticSnapshot | null>(null)
const optimisticJobs = ref<Record<string, SourceDiagnosticJob>>({})
const diagnosticQuery = ref('')
const diagnosticBusy = ref(false)
const loading = ref(true)
const notice = ref<Notice>(null)
const showImport = ref(false)
const editing = ref<string | null>(null)
const checking = ref<Record<string, boolean>>({})
const form = ref({ name: '', priority: '0', timeout: '10' })
const seenResults = new Set<string>()
const highWater = new Map<string, number>()
const latestRunAt = new Map<string, number>()
const expectedJob = new Map<string, string>()
let pageController: AbortController | null = null
let pageGeneration = 0

const enabledCount = computed(() => sources.value.filter((source) => source.enabled).length)
const existingIds = computed(() => new Set(sources.value.map((source) => source.id)))
const activeJobs = computed(() => {
  const jobs = (diagnostics.value?.jobs ?? []).filter((job) => job.status === 'queued' || job.status === 'running')
  const ids = new Set(jobs.map((job) => job.source_id))
  return [...jobs, ...Object.values(optimisticJobs.value).filter((job) => !ids.has(job.source_id))]
})
const activeJobCount = computed(() => activeJobs.value.length)
const activeBySource = computed(() => Object.fromEntries([...activeJobs.value].reverse().map((job) => [job.source_id, job])))
const latestBySource = computed(() => {
  const latest = new Map<string, SourceDiagnosticResult>()
  const activeSources = new Set(activeJobs.value.map((job) => job.source_id))
  for (const result of diagnostics.value?.results ?? []) {
    const barrier = activeSources.has(result.source_id) ? (latestRunAt.get(result.source_id) ?? -Infinity) : -Infinity
    if (result.tested_at < barrier) continue
    const previous = latest.get(result.source_id)
    if (!previous || (previous.stale !== false && result.stale === false) || (previous.stale === result.stale && previous.tested_at < result.tested_at)) latest.set(result.source_id, result)
  }
  return Object.fromEntries(latest)
})

const diagnosticEnabled = computed(() => activeJobCount.value > 0)
const refreshDiagnostics = usePolling(async (signal: AbortSignal) => {
  const snapshot = await sourceDiagnostics(signal)
  if (!signal.aborted) applySnapshot(snapshot)
}, 1500, diagnosticEnabled, false)

onMounted(() => { void loadSources() })
onBeforeUnmount(() => { pageGeneration += 1; pageController?.abort() })

function diagnosticKey(result: SourceDiagnosticResult): string { return [result.source_id, result.source_version, result.fingerprint ?? '', result.tested_at, result.status].join('\u0000') }
function resultScope(result: SourceDiagnosticResult): string { return result.source_id + '\u0000' + (result.fingerprint ?? '') }
function applySnapshot(snapshot: SourceDiagnosticSnapshot): void {
  const previous = seenResults.size ? seenResults : null
  let latestNotice: SourceDiagnosticResult | null = null
  for (const result of snapshot.results) {
    const key = diagnosticKey(result)
    if (previous && !previous.has(key) && ['busy', 'auth_required', 'rate_limited', 'timeout'].includes(result.status) && (!latestNotice || latestNotice.tested_at <= result.tested_at)) latestNotice = result
    seenResults.add(key)
  }
  for (const job of snapshot.jobs) {
    const expected = expectedJob.get(job.source_id)
    if (expected && job.job_id !== expected) continue
    if (job.status === 'queued' || job.status === 'running') latestRunAt.set(job.source_id, Math.max(latestRunAt.get(job.source_id) ?? -Infinity, job.submitted_at))
    if (job.result) {
      const scope = resultScope(job.result)
      highWater.set(scope, Math.max(highWater.get(scope) ?? -Infinity, job.result.tested_at))
      seenResults.add(diagnosticKey(job.result))
    }
  }
  while (seenResults.size > DIAGNOSTIC_SEEN_LIMIT) seenResults.delete(seenResults.values().next().value as string)
  const currentResults = currentDiagnosticSnapshotResults(snapshot.results, latestRunAt, highWater)
  const priorResults: Array<SourceDiagnosticResult & { stale: boolean }> = (diagnostics.value?.results ?? [])
    .filter((result) => !currentResults.some((item) => resultScope(item) === resultScope(result) && item.tested_at >= result.tested_at))
    .map((result) => ({ ...result, stale: Boolean(result.stale) }))
  const currentJobs = snapshot.jobs.filter((job) => !expectedJob.has(job.source_id) || expectedJob.get(job.source_id) === job.job_id)
  const nextOptimistic = { ...optimisticJobs.value }
  for (const [sourceId, optimistic] of Object.entries(nextOptimistic)) {
    const serverJob = snapshot.jobs.find((job) => job.source_id === sourceId && job.job_id === optimistic.job_id)
    const terminalResult = snapshot.results.find((result) => result.source_id === sourceId && result.tested_at >= (latestRunAt.get(sourceId) ?? Infinity))
    if (serverJob || terminalResult) delete nextOptimistic[sourceId]
  }
  optimisticJobs.value = nextOptimistic
  diagnostics.value = { ...snapshot, jobs: currentJobs, results: [...currentResults, ...priorResults].sort((a, b) => b.tested_at - a.tested_at).slice(0, DIAGNOSTIC_SEEN_LIMIT) }
  if (latestNotice) {
    notice.value = { tone: 'error', text: latestNotice.status === 'busy' ? latestNotice.source_id + ' 未检查：诊断容量已满，请稍后重试。' : latestNotice.source_id + '：' + (DIAGNOSTIC_TEXT[latestNotice.status] ?? latestNotice.message) }
  }
}

async function loadSources(): Promise<void> {
  pageController?.abort()
  const controller = new AbortController()
  pageController = controller
  const run = ++pageGeneration
  loading.value = true
  try {
    const report = await listSources()
    if (controller.signal.aborted || run !== pageGeneration) return
    sources.value = report.items
    notice.value = null
    const [healthResult, diagnosticResult] = await Promise.all([
      listSourceHealth(controller.signal).catch(() => null), sourceDiagnostics(controller.signal).catch(() => null),
    ])
    if (controller.signal.aborted || run !== pageGeneration) return
    if (healthResult) channels.value = Object.fromEntries(healthResult.sources.map((row) => [row.id, row]))
    else channels.value = {}
    if (diagnosticResult) applySnapshot(diagnosticResult)
  } catch (reason) {
    if (!controller.signal.aborted) notice.value = { tone: 'error', text: errorMessage(reason) }
  } finally {
    if (!controller.signal.aborted && run === pageGeneration) loading.value = false
  }
}

async function refreshHealth(): Promise<void> {
  try {
    const report: SourceHealthReport = await listSourceHealth()
    channels.value = Object.fromEntries(report.sources.map((row) => [row.id, row]))
  } catch { channels.value = {} }
}
function mergeResult(result: SourceDiagnosticResult): void {
  const key = resultScope(result)
  if (result.tested_at < (latestRunAt.get(result.source_id) ?? -Infinity)) return
  highWater.set(key, Math.max(highWater.get(key) ?? -Infinity, result.tested_at))
  const current = { ...result, stale: Boolean(result.stale) }
  diagnostics.value = diagnostics.value ? { ...diagnostics.value, results: [current, ...diagnostics.value.results.filter((item) => diagnosticKey(item) !== diagnosticKey(result) && resultScope(item) !== key)] } : null
  seenResults.add(diagnosticKey(result))
}
function optimisticallyQueue(source: SourceItem, jobId: string): void {
  const submittedAt = Date.now() / 1000
  optimisticJobs.value = { ...optimisticJobs.value, [source.id]: {
    job_id: jobId, source_id: source.id, source_version: source.plugin?.version ?? '', fingerprint: source.plugin?.sha256 ?? null,
    status: 'queued', stage: 'queue', query: diagnosticQuery.value.trim() || '周杰伦 晴天', submitted_at: submittedAt,
    deadline_at: submittedAt, batch_id: null,
  } }
}
async function checkOne(source: SourceItem): Promise<void> {
  diagnosticBusy.value = true
  const startTime = Date.now() / 1000 - 5
  try {
    const result = await startSourceDiagnostic(source.id, diagnosticQuery.value)
    if (result.mode === 'scheduled' || result.mode === 'deduplicated') {
      if (result.job_id) {
        expectedJob.set(source.id, result.job_id)
        latestRunAt.set(source.id, startTime)
        optimisticallyQueue(source, result.job_id)
      }
      notice.value = { tone: 'ok', text: (source.name || source.id) + ' 的诊断已' + (result.mode === 'deduplicated' ? '合并到现有任务' : '排队') + '，可取消。' }
      await refreshDiagnostics(true)
    } else if (result.mode === 'cooldown') {
      notice.value = { tone: 'ok', text: (source.name || source.id) + ' 最近已检查，' + (result.retry_after ?? 1) + ' 秒后可重试；本次实际查询：' + (result.actual_query ?? '—') }
      if (result.result) mergeResult({ ...result.result, stale: false })
    } else if (result.mode === 'not_started') {
      notice.value = { tone: 'error', text: (source.name || source.id) + ' 未排入诊断队列：' + result.status }
    }
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
  finally { diagnosticBusy.value = false }
}
async function checkAll(): Promise<void> {
  diagnosticBusy.value = true
  const startTime = Date.now() / 1000 - 5
  try {
    const report = await startSourceDiagnosticBatch(diagnosticQuery.value)
    for (const item of report.items) {
      if (item.job_id && (item.mode === 'scheduled' || item.mode === 'deduplicated')) {
        expectedJob.set(item.source_id, item.job_id)
        latestRunAt.set(item.source_id, startTime)
        const source = sources.value.find((entry) => entry.id === item.source_id)
        if (source) optimisticallyQueue(source, item.job_id)
      }
    }
    const scheduled = report.items.filter((item) => item.mode === 'scheduled' && item.job_id).length
    notice.value = {
      tone: report.not_started ? 'error' : 'ok',
      text: (scheduled ? '批量诊断已提交：' : '批量诊断：') + report.scheduled + ' 个排队，' + report.deduplicated + ' 个合并，' + report.cooldown + ' 个在冷却，' + report.not_started + ' 个未排队。',
    }
    await refreshDiagnostics(true)
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
  finally { diagnosticBusy.value = false }
}
async function cancelDiagnostic(job: SourceDiagnosticJob): Promise<void> {
  try {
    await cancelSourceDiagnosticJob(job.job_id)
    notice.value = { tone: 'ok', text: '已请求取消 ' + job.source_id + ' 的诊断。' }
    await refreshDiagnostics(true)
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
}
async function checkLossless(source: SourceItem): Promise<void> {
  checking.value = { ...checking.value, [source.id]: true }
  try {
    const result = await checkSourceLossless(source.id)
    await refreshHealth()
    notice.value = { tone: 'ok', text: (source.name || source.id) + ' 的无损检查完成：' + LOSSLESS_TEXT[result.status] }
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
  finally { checking.value = { ...checking.value, [source.id]: false } }
}
async function toggleEnabled(source: SourceItem): Promise<void> {
  try {
    const saved = await updateSource(source.id, { enabled: !source.enabled })
    replace(saved)
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
}
function replace(item: SourceItem): void { sources.value = sources.value.map((source) => source.id === item.id ? item : source) }
function startEditing(source: SourceItem): void { editing.value = source.id; form.value = { name: source.name ?? '', priority: String(source.priority), timeout: String(source.timeout) } }
async function saveEditing(source: SourceItem): Promise<void> {
  const priority = Number.parseInt(form.value.priority, 10)
  const timeout = Number(form.value.timeout)
  if (!Number.isInteger(priority) || priority < -1000 || priority > 1000) { notice.value = { tone: 'error', text: '优先级必须是 -1000 到 1000 之间的整数' }; return }
  if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 300) { notice.value = { tone: 'error', text: '超时必须是 0.1 到 300 秒之间的数字' }; return }
  try {
    const saved = await updateSource(source.id, { name: form.value.name.trim() || null, priority, timeout })
    replace(saved)
    editing.value = null
    notice.value = { tone: 'ok', text: '已更新 ' + (source.name || source.id) + reloadNote(saved.reload) }
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
}
async function removeSource(source: SourceItem): Promise<void> {
  if (!window.confirm('确定删除音源「' + (source.name || source.id) + '」吗？安装的脚本也会一并移除。')) return
  try {
    const removed = await deleteSource(source.id)
    sources.value = sources.value.filter((item) => item.id !== source.id)
    notice.value = { tone: 'ok', text: '已删除 ' + source.id + (removed.uninstalled?.length ? '，并卸载 ' + removed.uninstalled.length + ' 个脚本版本' : '') + reloadNote(removed.reload) }
  } catch (reason) { notice.value = { tone: 'error', text: errorMessage(reason) } }
}
function losslessLabel(row: SourceHealthRow): string { return LOSSLESS_TEXT[row.lossless_status] }
function losslessClass(row: SourceHealthRow): string { return row.lossless_stale && row.lossless_status !== 'unknown' ? 'bg-neutral-800 text-neutral-400' : LOSSLESS_CLASS[row.lossless_status] }
function losslessTitle(row: SourceHealthRow): string {
  const statement = row.lossless_status === 'lossless' ? '实测该渠道可以提供无损音质' : row.lossless_status === 'lossy' ? '实测该渠道只提供有损音质' : '还没有实测过该渠道'
  const when = row.lossless_checked_at === null ? '尚未实测' : '实测于 ' + dateTime(row.lossless_checked_at * 1000)
  return statement + ' · ' + when + (row.lossless_stale && row.lossless_status !== 'unknown' ? '，结论已过期' : '')
}
function egressText(source: SourceItem): string {
  const egress = source.plugin?.egress
  if (!egress) return '脚本尚未安装，出口策略未知'
  const parts = [(egress.allowed_hosts?.length ?? 0) + ' 个域名', '端口 ' + (egress.allowed_ports?.length ? egress.allowed_ports : [443]).join('/')]
  if (egress.allow_insecure_http) parts.push('允许明文')
  if (egress.allow_ip_hosts) parts.push('允许 IP')
  if (egress.allow_any_host) parts.push('允许任意域名')
  return parts.join(' · ')
}
function ms(value: number | null): string { return value == null ? '—' : value + ' ms' }
function waited(job: SourceDiagnosticJob): number { return Math.max(0, Math.floor(Date.now() / 1000 - job.submitted_at)) }
function dateTime(value: number | string | null): string {
  if (value === null) return '—'
  const date = new Date(typeof value === 'number' ? value : value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}
</script>

<style scoped>
.field-control { min-height: 2.65rem; border: 1px solid var(--glass-border); border-radius: .75rem; padding: .55rem .8rem; background: color-mix(in srgb, var(--color-neutral-900) 55%, transparent); color: var(--color-neutral-100); transition: border-color 150ms; }
.field-control:focus { border-color: var(--color-accent-400); outline: none; }
.field-control:disabled { cursor: not-allowed; opacity: .55; }
.action-primary, .action-secondary, .action-danger { display: inline-flex; align-items: center; justify-content: center; gap: .45rem; min-height: 2.45rem; border-radius: .75rem; padding: .55rem .8rem; font-size: .875rem; font-weight: 600; transition: background-color 150ms, border-color 150ms, opacity 150ms; }
.action-primary { background: var(--color-accent-500); color: var(--color-neutral-100); }
.action-primary:hover:not(:disabled) { background: var(--color-accent-600); }
.action-secondary { border: 1px solid var(--glass-border); background: color-mix(in srgb, var(--color-neutral-900) 45%, transparent); color: var(--color-neutral-200); }
.action-secondary:hover:not(:disabled) { background: var(--color-neutral-800); }
.action-danger { border: 1px solid color-mix(in srgb, var(--color-danger) 22%, transparent); background: color-mix(in srgb, var(--color-danger) 9%, transparent); color: var(--color-danger); }
.action-danger:hover:not(:disabled) { background: color-mix(in srgb, var(--color-danger) 18%, transparent); }
.action-primary:disabled, .action-secondary:disabled, .action-danger:disabled { cursor: not-allowed; opacity: .5; }
</style>
