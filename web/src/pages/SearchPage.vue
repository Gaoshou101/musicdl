<template>
  <section class="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
    <header class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">音乐搜索</h1>
        <p class="mt-1.5 max-w-2xl text-sm leading-relaxed text-neutral-400">用与机器人相同的音源注册表搜索并试下载，核对真实可用的音源、格式与文件大小。</p>
      </div>
      <RouterLink to="/dashboard/downloads" class="text-sm text-accent-300 hover:underline">查看下载历史与换源过程 →</RouterLink>
    </header>

    <form class="glass rounded-2xl p-4 sm:p-5" @submit.prevent="runSearch(query)">
      <div class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto_auto]">
        <label class="sr-only" for="search-query">搜索关键词</label>
        <div class="relative">
          <MagnifyingGlass :size="19" class="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-neutral-500" />
          <input id="search-query" v-model="query" type="search" autocomplete="off" placeholder="输入歌曲名称或艺术家…"
            aria-label="搜索关键词" :disabled="searching" class="field-control w-full pl-11 text-base" />
        </div>
        <label class="sr-only" for="search-limit">结果条数</label>
        <select id="search-limit" v-model.number="limit" aria-label="结果条数" class="field-control min-w-28">
          <option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option><option :value="100">100 条</option>
        </select>
        <button type="submit" :disabled="searching || !query.trim()" class="action-primary min-w-28 justify-center">
          <span v-if="searching" class="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden="true"></span>
          <MagnifyingGlass v-else :size="18" weight="bold" />{{ searching ? '搜索中' : '搜索' }}
        </button>
      </div>
    </form>

    <p v-if="error" role="alert" class="rounded-xl border border-danger/20 bg-danger/10 px-4 py-3 text-sm text-danger">{{ error }}</p>

    <section v-if="report && !error" class="glass rounded-2xl p-4 sm:p-5" aria-label="搜索状态">
      <div class="flex items-start gap-3">
        <CheckCircle v-if="report.count > 0" :size="20" weight="fill" class="mt-0.5 shrink-0 text-success" />
        <WarningCircle v-else :size="20" weight="fill" class="mt-0.5 shrink-0 text-warning" />
        <p class="text-sm leading-relaxed">「{{ report.query }}」共 {{ report.total }} 个结果，本次返回 {{ report.count }} 条
          <span class="text-neutral-500"> · 注册表版本 <code class="font-mono">{{ report.version }}</code></span>
        </p>
      </div>
      <div v-if="report.sources.length" class="mt-3 flex flex-wrap gap-2">
        <span v-if="sharedSources.length" class="rounded-md px-2 py-1 text-xs" :class="sharedFailures.length ? 'bg-warning/15 text-warning' : 'bg-neutral-800/70 text-neutral-300'"
          :title="sharedSources.map((source) => source.id + ': ' + source.status).join('、')">
          共享目录检索 · {{ sharedSources.length }} 个渠道共用同一份结果（各 {{ sharedSources[0]?.count ?? 0 }} 条）
          <template v-if="sharedFailures.length"> · 异常 {{ sharedFailures.map((source) => source.id).join('、') }}</template>
        </span>
        <span v-for="source in independentSources" :key="source.id" class="rounded-md px-2 py-1 font-mono text-xs"
          :class="source.status === 'ok' ? 'bg-neutral-800 text-neutral-300' : 'bg-warning/15 text-warning'" :title="source.status">
          {{ source.id }}: {{ source.count }}<template v-if="source.status !== 'ok'">（{{ source.status }}）</template>
        </span>
      </div>
    </section>

    <section v-if="candidates.length" aria-labelledby="search-results-title" class="space-y-3">
      <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="search-results-title" class="text-lg font-semibold">搜索结果 <span class="text-sm font-normal text-neutral-500">{{ filteredRows.length }} 条</span></h2>
          <p class="mt-1 text-xs text-neutral-500">按录音聚合，渠道筛选会显示所有能提供该歌曲的来源。</p>
        </div>
        <div class="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:justify-end">
          <label class="sr-only" for="source-filter">按音源筛选</label>
          <select id="source-filter" v-model="sourceFilter" aria-label="按音源筛选" class="field-control min-w-32">
            <option value="all">全部渠道</option><option v-for="id in sourceIds" :key="id" :value="id">{{ id }}</option>
          </select>
          <label class="sr-only" for="format-filter">按音频格式筛选</label>
          <select id="format-filter" v-model="formatFilter" aria-label="按音频格式筛选" class="field-control min-w-32">
            <option value="all">全部格式</option><option v-for="format in formats" :key="format" :value="format">{{ format.toUpperCase() }}</option>
          </select>
          <label class="sr-only" for="result-sort">结果排序</label>
          <select id="result-sort" v-model="sortBy" aria-label="结果排序" class="field-control min-w-36">
            <option value="default">来源优先</option><option value="title">歌曲名</option><option value="artist">艺术家</option><option value="duration">时长</option><option value="size">文件大小</option>
          </select>
          <button type="button" :aria-pressed="probeEnabled" title="探测当前筛选结果前 10 条的真实文件大小" class="action-secondary justify-center" @click="probeEnabled = !probeEnabled">
            <Waveform :size="17" />{{ probeEnabled ? '关闭探测' : '探测大小' }}
          </button>
        </div>
      </div>

      <p v-if="probeError" role="status" class="rounded-lg border border-warning/20 bg-warning/10 px-3 py-2 text-xs text-warning">文件大小探测未完成：{{ probeError }}</p>
      <div v-if="!pageRows.length" class="glass rounded-xl p-8 text-center text-sm text-neutral-400">当前筛选条件下没有结果。</div>
      <article v-for="{ candidate, channels } in pageRows" :key="rowKey(candidate)" class="glass rounded-2xl p-4 sm:p-5">
        <div class="flex items-start gap-3 sm:gap-4">
          <div class="grid size-11 shrink-0 place-items-center rounded-xl bg-accent-500/10 text-accent-300"><MusicNotes :size="22" weight="duotone" /></div>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <h3 class="min-w-0 break-words font-medium">{{ candidate.title }}</h3>
              <span v-if="candidate.platform" class="rounded bg-accent-500/15 px-1.5 py-0.5 font-mono text-[10px] uppercase text-accent-300" :title="`结果来自 ${candidate.platform} 目录`">{{ candidate.platform }}</span>
            </div>
            <p class="mt-1 break-words text-sm text-neutral-400">{{ candidate.artist || '未知艺术家' }}<template v-if="candidate.album"> · {{ candidate.album }}</template></p>
            <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-neutral-500">
              <span v-if="candidate.duration">{{ formatDuration(candidate.duration) }}</span>
              <span v-if="candidate.bitrate">{{ candidate.bitrate }} kbps</span>
              <span v-if="candidate.format">{{ candidate.format.toUpperCase() }}</span>
              <span>{{ sizeText(candidate) }}</span>
            </div>
            <p class="mt-2 break-all font-mono text-[11px] text-neutral-500">优先 {{ candidate.source_id }} v{{ candidate.source_version }} · {{ shortHash(candidate.item_id, 20) }}</p>
            <p v-if="channels.length > 1" class="mt-1 text-xs text-neutral-500" :title="channels.join('、')">{{ channels.length }} 个渠道可下载：{{ channels.slice(0, 4).join('、') }}<template v-if="channels.length > 4"> 等 {{ channels.length }} 个</template></p>
          </div>
          <button type="button" title="下载到服务端媒体目录" aria-label="下载到服务端媒体目录" :disabled="downloadState(candidate).status === 'busy'" class="icon-action shrink-0 text-accent-300" @click="download(candidate)">
            <span v-if="downloadState(candidate).status === 'busy'" class="size-4 animate-spin rounded-full border-2 border-current border-t-transparent"></span>
            <Download v-else :size="19" weight="duotone" />
          </button>
        </div>
        <p v-if="downloadState(candidate).status === 'error'" role="alert" class="mt-3 text-sm text-danger">下载失败：{{ downloadState(candidate).error }}</p>
        <div v-if="downloadState(candidate).status === 'done' && downloadState(candidate).report" class="mt-4 space-y-1 border-t border-neutral-800 pt-3 text-xs text-neutral-400">
          <p>下载成功：{{ formatBytes(downloadState(candidate).report?.size_bytes) }} · {{ downloadState(candidate).report?.media_type }} · 语言 {{ downloadState(candidate).report?.language }} · 请求 {{ shortHash(downloadState(candidate).report?.request_id, 12) }}</p>
          <p v-if="downgrade(downloadState(candidate).report)" class="text-warning">请求 {{ downgrade(downloadState(candidate).report)?.asked }}，实际 {{ downgrade(downloadState(candidate).report)?.served }}{{ downgrade(downloadState(candidate).report)?.lossy ? '（有损）' : '' }}</p>
          <p v-if="downloadState(candidate).report?.history_warning" role="status" class="text-warning">文件已下载，但历史记录保存不完整，请检查应用数据目录。</p>
          <p v-if="downloadState(candidate).report?.fallback_from" class="text-warning">原音源 {{ downloadState(candidate).report?.fallback_from }} 未能取到音频，已改用 {{ downloadState(candidate).report?.source_id }} 下载。</p>
          <p class="break-all font-mono">sha256 {{ downloadState(candidate).report?.sha256 }}</p>
          <RouterLink class="block text-accent-300 hover:underline" :to="{ path: '/dashboard/downloads', query: { id: downloadState(candidate).report?.request_id } }">查看本次下载记录</RouterLink>
          <a class="break-all text-accent-300 hover:underline" :href="mediaUrl(downloadState(candidate).report?.relative_path ?? '')" target="_blank" rel="noreferrer">{{ downloadState(candidate).report?.relative_path }}</a>
        </div>
      </article>
      <nav v-if="filteredRows.length > pageSize" aria-label="搜索结果分页" class="flex items-center justify-between gap-3 rounded-xl border border-neutral-800 bg-neutral-900/25 px-3 py-2">
        <button type="button" class="action-secondary" :disabled="page === 0" @click="page--">上一页</button>
        <span class="text-xs text-neutral-500">第 {{ page + 1 }} / {{ pageCount }} 页 · 每页 {{ pageSize }} 条</span>
        <button type="button" class="action-secondary" :disabled="page + 1 >= pageCount" @click="page++">下一页</button>
      </nav>
    </section>

    <section v-if="!report && !error && !searching" class="glass rounded-2xl px-6 py-12 text-center">
      <MagnifyingGlass :size="38" class="mx-auto text-neutral-500" />
      <h2 class="mt-3 font-semibold">开始搜索音乐</h2>
      <p class="mx-auto mt-1 max-w-lg text-sm text-neutral-400">输入歌曲或艺术家名称。列表标注目录平台、音源版本和可下载渠道。</p>
    </section>
    <section v-if="report && !candidates.length && !error" class="glass rounded-2xl px-6 py-10 text-center">
      <h2 class="font-semibold">没有找到结果</h2><p class="mt-2 text-sm text-neutral-400">换个关键词重试；也可以到音源管理确认脚本已安装并启用。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { PhCheckCircle as CheckCircle, PhDownload as Download, PhMagnifyingGlass as MagnifyingGlass, PhMusicNotes as MusicNotes, PhWarningCircle as WarningCircle, PhWaveform as Waveform } from '@phosphor-icons/vue'
import {
  downloadCandidate, errorMessage, mediaUrl, probeCandidateSizes, searchCandidates,
  type Candidate, type DownloadReport, type SearchReport,
} from '@/lib/api'
import { formatBytes, formatDuration, shortHash } from '@/lib/format'

type RowState = { status: 'idle' | 'busy' | 'done' | 'error'; report?: DownloadReport; error?: string }
const LOSSLESS = new Set(['flac', 'flac24bit', 'alac', 'ape', 'wav', 'aiff'])
const LOSSY = new Set(['128k', '192k', '320k', 'mp3', 'aac', 'ogg', 'opus'])
const route = useRoute()
const query = ref('')
const limit = ref(20)
const searching = ref(false)
const report = ref<SearchReport | null>(null)
const error = ref('')
const sourceFilter = ref('all')
const formatFilter = ref('all')
const sortBy = ref('default')
const page = ref(0)
const pageSize = 20
const probeEnabled = ref(false)
const probedSizes = ref<Record<string, number | null>>({})
const probing = ref<Record<string, boolean>>({})
const probeError = ref('')
const downloads = ref<Record<string, RowState>>({})
const activeDownloads = new Set<string>()
let searchController: AbortController | null = null
let generation = 0

const rowKey = (candidate: Candidate) => JSON.stringify([candidate.source_id, candidate.item_id])
const candidates = computed(() => report.value?.candidates ?? [])
const offersFor = (index: number, candidate: Candidate) => report.value?.offers?.[index]?.length ? report.value.offers[index] : [candidate.source_id]
const sourceIds = computed(() => [...new Set(candidates.value.flatMap((candidate, index) => offersFor(index, candidate)))].sort())
const formats = computed(() => [...new Set(candidates.value.map((candidate) => (candidate.format ?? '').trim().toLowerCase()).filter(Boolean))].sort())
const sharedSources = computed(() => (report.value?.sources ?? []).filter((source) => source.catalogue))
const sharedFailures = computed(() => sharedSources.value.filter((source) => source.status !== 'ok'))
const independentSources = computed(() => (report.value?.sources ?? []).filter((source) => !source.catalogue))
const filteredRows = computed(() => {
  const rows = candidates.value.map((candidate, index) => ({ candidate, channels: offersFor(index, candidate) }))
    .filter(({ candidate, channels }) => (sourceFilter.value === 'all' || channels.includes(sourceFilter.value)) &&
      (formatFilter.value === 'all' || (candidate.format ?? '').trim().toLowerCase() === formatFilter.value))
  if (sortBy.value !== 'default') {
    rows.sort((left, right) => {
      const a = left.candidate
      const b = right.candidate
      if (sortBy.value === 'duration' || sortBy.value === 'size') {
        const field = sortBy.value === 'duration' ? 'duration' : 'size'
        return (a[field] ?? Number.MAX_SAFE_INTEGER) - (b[field] ?? Number.MAX_SAFE_INTEGER)
      }
      return String(a[sortBy.value as 'title' | 'artist'] ?? '').localeCompare(String(b[sortBy.value as 'title' | 'artist'] ?? ''), 'zh-CN')
    })
  }
  return rows
})
const pageCount = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize)))
const pageRows = computed(() => filteredRows.value.slice(page.value * pageSize, (page.value + 1) * pageSize))

watch([sourceFilter, formatFilter, sortBy], () => { page.value = 0 })
watch(pageCount, (count) => { if (page.value >= count) page.value = Math.max(0, count - 1) })

function sizeText(candidate: Candidate): string {
  if (probing.value[rowKey(candidate)]) return '正在探测大小…'
  const measured = probedSizes.value[rowKey(candidate)]
  const size = measured === undefined ? candidate.size : measured
  return size == null ? '' : formatBytes(size)
}

function downloadState(candidate: Candidate): RowState {
  return downloads.value[rowKey(candidate)] ?? { status: 'idle' }
}

function downgrade(value?: DownloadReport): { asked: string; served: string; lossy: boolean } | null {
  const asked = (value?.requested_quality ?? '').trim()
  const served = (value?.actual_quality ?? '').trim()
  if (!LOSSLESS.has(asked.toLowerCase()) || !served || served.toLowerCase() === asked.toLowerCase()) return null
  return { asked, served, lossy: LOSSY.has(served.toLowerCase()) }
}

async function runSearch(text: string): Promise<void> {
  const trimmed = text.trim()
  if (!trimmed) return
  searchController?.abort()
  const controller = new AbortController()
  searchController = controller
  const currentGeneration = ++generation
  report.value = null
  downloads.value = {}
  probedSizes.value = {}
  probing.value = {}
  probeError.value = ''
  sourceFilter.value = 'all'
  formatFilter.value = 'all'
  sortBy.value = 'default'
  page.value = 0
  searching.value = true
  error.value = ''
  try {
    const result = await searchCandidates(trimmed, limit.value, controller.signal)
    if (controller.signal.aborted || currentGeneration !== generation) return
    report.value = result
  } catch (reason) {
    if (controller.signal.aborted || currentGeneration !== generation) return
    report.value = null
    error.value = errorMessage(reason)
  } finally {
    if (searchController === controller && !controller.signal.aborted) {
      searchController = null
      searching.value = false
    }
  }
}

// A dashboard deep link is the same handoff the old panel supported.
if (typeof route.query.q === 'string' && route.query.q.trim()) {
  query.value = route.query.q
  void runSearch(route.query.q)
}

let probeController: AbortController | null = null
watch(() => [probeEnabled.value, searching.value, pageRows.value.map(({ candidate }) => rowKey(candidate)).join('|')] as const,
  async ([enabled, isSearching], _previous, onCleanup) => {
    probeController?.abort()
    probeController = null
    probedSizes.value = {}
    probing.value = {}
    probeError.value = ''
    if (!enabled || isSearching || pageRows.value.length === 0) return
    const selected = pageRows.value.slice(0, 10).map(({ candidate }) => candidate)
    const controller = new AbortController()
    probeController = controller
    let active = true
    onCleanup(() => { active = false; controller.abort() })
    probing.value = Object.fromEntries(selected.map((candidate) => [rowKey(candidate), true]))
    try {
      const result = await probeCandidateSizes(selected, controller.signal)
      if (!active || controller.signal.aborted) return
      probedSizes.value = Object.fromEntries(selected.map((candidate) => [rowKey(candidate), result[candidate.source_id]?.[candidate.item_id]?.size ?? null]))
    } catch (reason) {
      if (active && !controller.signal.aborted) probeError.value = errorMessage(reason)
    } finally {
      if (active) probing.value = {}
    }
  }, { immediate: true, flush: 'post' })

async function download(candidate: Candidate): Promise<void> {
  const key = rowKey(candidate)
  const activeKey = JSON.stringify([candidate.source_id, candidate.source_version, candidate.item_id])
  if (activeDownloads.has(activeKey)) {
    error.value = '该歌曲正在下载，请等待当前任务完成。'
    return
  }
  activeDownloads.add(activeKey)
  const currentGeneration = generation
  downloads.value = { ...downloads.value, [key]: { status: 'busy' } }
  try {
    const result = await downloadCandidate(candidate, report.value?.query ?? query.value)
    if (generation !== currentGeneration) return
    downloads.value = { ...downloads.value, [key]: { status: 'done', report: result } }
  } catch (reason) {
    if (generation === currentGeneration) downloads.value = { ...downloads.value, [key]: { status: 'error', error: errorMessage(reason) } }
  } finally {
    activeDownloads.delete(activeKey)
  }
}

onBeforeUnmount(() => {
  generation += 1
  searchController?.abort()
  probeController?.abort()
})
</script>

<style scoped>
.field-control { min-height: 2.75rem; border: 1px solid var(--glass-border); border-radius: 0.75rem; padding: 0.625rem 0.875rem; background: color-mix(in srgb, var(--color-neutral-900) 58%, transparent); color: var(--color-neutral-100); transition: border-color 150ms, background-color 150ms; }
.field-control:focus { border-color: var(--color-accent-400); outline: none; }
.field-control:disabled { cursor: not-allowed; opacity: .55; }
.field-control option { background: var(--color-neutral-900); }
.action-primary, .action-secondary { display: inline-flex; align-items: center; gap: .5rem; border-radius: .75rem; padding: .625rem .875rem; font-size: .875rem; font-weight: 600; transition: background-color 150ms, border-color 150ms, opacity 150ms; }
.action-primary { background: var(--color-accent-500); color: var(--color-neutral-100); }
.action-primary:hover:not(:disabled) { background: var(--color-accent-600); }
.action-secondary { border: 1px solid var(--glass-border); background: color-mix(in srgb, var(--color-neutral-900) 45%, transparent); color: var(--color-neutral-200); }
.action-secondary:hover:not(:disabled) { background: var(--color-neutral-800); }
.action-primary:disabled, .action-secondary:disabled { cursor: not-allowed; opacity: .5; }
.icon-action { display: inline-grid; min-width: 2.5rem; min-height: 2.5rem; place-items: center; border: 1px solid var(--glass-border); border-radius: .75rem; background: color-mix(in srgb, var(--color-neutral-900) 45%, transparent); color: var(--color-neutral-200); transition: background-color 150ms; }
.icon-action:hover:not(:disabled) { background: var(--color-neutral-800); }
.icon-action:disabled { cursor: not-allowed; opacity: .5; }
</style>
