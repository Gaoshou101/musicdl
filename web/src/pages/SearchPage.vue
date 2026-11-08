<template>
  <section class="admin-page music-search-page">
    <header class="search-hero" :class="{ 'search-hero--results': candidates.length > 0 }">
      <div class="search-hero__copy">
        <h1 class="page-title search-title">音乐搜索</h1>
        <p class="page-description search-intro">搜索歌曲与艺术家，找到可下载的音乐。</p>
        <RouterLink to="/dashboard/downloads" class="history-link">查看下载历史与换源过程 <span aria-hidden="true">↗</span></RouterLink>
      </div>
      <div class="hero-art" aria-hidden="true">
        <div class="hero-art__glow"></div>
        <div class="hero-art__sleeve">
          <span class="hero-art__sleeve-orbit"></span>
          <span class="hero-art__sleeve-sun"></span>
        </div>
        <div class="hero-art__record"><span></span></div>
      </div>
    </header>

    <form class="search-form" @submit.prevent="runSearch(query)">
      <div class="search-controls">
        <label class="sr-only" for="search-query">搜索关键词</label>
        <div class="search-input-wrap">
          <MagnifyingGlass :size="20" class="search-input-icon" aria-hidden="true" />
          <input id="search-query" v-model="query" type="search" autocomplete="off" placeholder="输入歌曲名称或艺术家…"
            aria-label="搜索关键词" :disabled="searching" class="field-control search-input" />
        </div>
        <label class="sr-only" for="search-limit">结果条数</label>
        <select id="search-limit" v-model.number="limit" aria-label="结果条数" class="field-control limit-select">
          <option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option><option :value="100">100 条</option>
        </select>
        <button type="submit" :disabled="searching || !query.trim()" class="action-primary search-submit">
          <span v-if="searching" class="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden="true"></span>
          <MagnifyingGlass v-else :size="18" weight="bold" />{{ searching ? '搜索中' : '搜索' }}
        </button>
      </div>
    </form>

    <p v-if="error" role="alert" class="inline-message inline-message--danger">{{ error }}</p>

    <section v-if="report && !error" class="result-summary glass" aria-label="搜索状态">
      <div class="result-summary__main">
        <CheckCircle v-if="report.count > 0" :size="21" weight="fill" class="shrink-0 text-success" />
        <WarningCircle v-else :size="21" weight="fill" class="shrink-0 text-warning" />
        <p><strong>「{{ report.query }}」</strong>共 {{ report.total }} 个结果，本次返回 {{ report.count }} 条
          <span class="registry-version">注册表版本 <code>{{ report.version }}</code></span>
        </p>
      </div>
      <div v-if="report.sources.length" class="source-badges">
        <span v-if="sharedSources.length" class="source-badge" :class="sharedFailures.length ? 'source-badge--warning' : ''"
          :title="sharedSources.map((source) => source.id + ': ' + source.status).join('、')">
          共享目录检索 · {{ sharedSources.length }} 个渠道共用同一份结果（各 {{ sharedSources[0]?.count ?? 0 }} 条）
          <template v-if="sharedFailures.length"> · 异常 {{ sharedFailures.map((source) => source.id).join('、') }}</template>
        </span>
        <span v-for="source in independentSources" :key="source.id" class="source-badge source-badge--mono"
          :class="source.status !== 'ok' ? 'source-badge--warning' : ''" :title="source.status">
          {{ source.id }}: {{ source.count }}<template v-if="source.status !== 'ok'">（{{ source.status }}）</template>
        </span>
      </div>
    </section>

    <section v-if="candidates.length" aria-labelledby="search-results-title" class="results-section">
      <div class="results-head">
        <div>
          <h2 id="search-results-title">搜索结果 <span>{{ filteredRows.length }} 条</span></h2>
          <p class="results-caption">按录音聚合，渠道筛选会显示所有能提供该歌曲的来源。</p>
        </div>
        <div class="result-tools">
          <label class="sr-only" for="source-filter">按音源筛选</label>
          <select id="source-filter" v-model="sourceFilter" aria-label="按音源筛选" class="field-control">
            <option value="all">全部渠道</option><option v-for="id in sourceIds" :key="id" :value="id">{{ id }}</option>
          </select>
          <label class="sr-only" for="format-filter">按音频格式筛选</label>
          <select id="format-filter" v-model="formatFilter" aria-label="按音频格式筛选" class="field-control">
            <option value="all">全部格式</option><option v-for="format in formats" :key="format" :value="format">{{ format.toUpperCase() }}</option>
          </select>
          <label class="sr-only" for="result-sort">结果排序</label>
          <select id="result-sort" v-model="sortBy" aria-label="结果排序" class="field-control">
            <option value="default">来源优先</option><option value="title">歌曲名</option><option value="artist">艺术家</option><option value="duration">时长</option><option value="size">文件大小</option>
          </select>
          <button type="button" :aria-pressed="probeEnabled" title="探测当前筛选结果前 10 条的真实文件大小" class="action-secondary probe-button" @click="probeEnabled = !probeEnabled">
            <Waveform :size="17" />{{ probeEnabled ? '关闭探测' : '探测大小' }}
          </button>
        </div>
      </div>

      <p v-if="probeError" role="status" class="inline-message inline-message--warning">文件大小探测未完成：{{ probeError }}</p>
      <div v-if="!pageRows.length" class="filtered-empty glass">当前筛选条件下没有结果。</div>
      <article v-for="{ candidate, channels } in pageRows" :key="rowKey(candidate)" class="result-card glass">
        <div class="result-card__main">
          <div class="record-art" :class="artworkClass(candidate)" aria-hidden="true">
            <span class="record-art__arc"></span><span class="record-art__sun"></span>
          </div>
          <div class="result-card__copy">
            <div class="result-card__title-row">
              <h3>{{ candidate.title }}</h3>
              <span v-if="candidate.platform" class="platform-chip" :title="`结果来自 ${candidate.platform} 目录`">{{ candidate.platform }}</span>
            </div>
            <p class="result-card__artist">{{ candidate.artist || '未知艺术家' }}<template v-if="candidate.album"> <span>·</span> {{ candidate.album }}</template></p>
            <div class="track-metadata">
              <span v-if="candidate.duration">{{ formatDuration(candidate.duration) }}</span>
              <span v-if="candidate.bitrate">{{ candidate.bitrate }} kbps</span>
              <span v-if="candidate.format">{{ candidate.format.toUpperCase() }}</span>
              <span v-if="sizeText(candidate)">{{ sizeText(candidate) }}</span>
            </div>
            <p class="source-reference">优先 {{ candidate.source_id }} v{{ candidate.source_version }} · {{ shortHash(candidate.item_id, 20) }}</p>
            <p v-if="channels.length > 1" class="available-channels" :title="channels.join('、')">{{ channels.length }} 个渠道可下载：{{ channels.slice(0, 4).join('、') }}<template v-if="channels.length > 4"> 等 {{ channels.length }} 个</template></p>
          </div>
          <button type="button" title="下载到服务端媒体目录" aria-label="下载到服务端媒体目录" :disabled="downloadState(candidate).status === 'busy'" class="icon-action download-action" @click="download(candidate)">
            <span v-if="downloadState(candidate).status === 'busy'" class="size-4 animate-spin rounded-full border-2 border-current border-t-transparent"></span>
            <Download v-else :size="19" weight="duotone" />
          </button>
        </div>
        <p v-if="downloadState(candidate).status === 'error'" role="alert" class="download-error">下载失败：{{ downloadState(candidate).error }}</p>
        <div v-if="downloadState(candidate).status === 'done' && downloadState(candidate).report" class="download-report">
          <p>下载成功：{{ formatBytes(downloadState(candidate).report?.size_bytes) }} · {{ downloadState(candidate).report?.media_type }} · 语言 {{ downloadState(candidate).report?.language }} · 请求 {{ shortHash(downloadState(candidate).report?.request_id, 12) }}</p>
          <p v-if="downgrade(downloadState(candidate).report)" class="text-warning">请求 {{ downgrade(downloadState(candidate).report)?.asked }}，实际 {{ downgrade(downloadState(candidate).report)?.served }}{{ downgrade(downloadState(candidate).report)?.lossy ? '（有损）' : '' }}</p>
          <p v-if="downloadState(candidate).report?.history_warning" role="status" class="text-warning">文件已下载，但历史记录保存不完整，请检查应用数据目录。</p>
          <p v-if="downloadState(candidate).report?.fallback_from" class="text-warning">原音源 {{ downloadState(candidate).report?.fallback_from }} 未能取到音频，已改用 {{ downloadState(candidate).report?.source_id }} 下载。</p>
          <p class="break-all font-mono">sha256 {{ downloadState(candidate).report?.sha256 }}</p>
          <RouterLink class="report-link" :to="{ path: '/dashboard/downloads', query: { id: downloadState(candidate).report?.request_id } }">查看本次下载记录</RouterLink>
          <a class="report-link break-all" :href="mediaUrl(downloadState(candidate).report?.relative_path ?? '')" target="_blank" rel="noreferrer">{{ downloadState(candidate).report?.relative_path }}</a>
        </div>
      </article>
      <nav v-if="filteredRows.length > pageSize" aria-label="搜索结果分页" class="pagination glass">
        <button type="button" class="action-secondary" :disabled="page === 0" @click="page--">上一页</button>
        <span>第 {{ page + 1 }} / {{ pageCount }} 页 · 每页 {{ pageSize }} 条</span>
        <button type="button" class="action-secondary" :disabled="page + 1 >= pageCount" @click="page++">下一页</button>
      </nav>
    </section>

    <section v-if="report && !candidates.length && !error" class="no-results glass">
      <div class="no-results__mark" aria-hidden="true"><MagnifyingGlass :size="22" /></div>
      <div><h2>没有找到结果</h2>
        <p>换个关键词重试；也可以到音源管理确认脚本已安装并启用。</p></div>
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
function artworkClass(candidate: Candidate): string {
  const seed = `${candidate.source_id}:${candidate.item_id}`
  let value = 0
  for (const character of seed) value = (value * 31 + character.charCodeAt(0)) >>> 0
  return `record-art--${['ember', 'tide', 'orchid'][value % 3]}`
}
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
.music-search-page { color: var(--color-neutral-100); }
.search-hero { display: grid; grid-template-columns: minmax(0, 1fr) 148px; align-items: center; gap: 28px; min-height: 136px; }
.search-hero__copy { min-width: 0; }
.search-title { margin: 0; }
.search-intro { margin: 7px 0 0; }
.history-link { display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; color: var(--color-accent-300); font-size: 12px; font-weight: 550; text-decoration: none; transition: color var(--dur-fast) var(--ease-out); }
.history-link:hover { color: var(--color-accent-200); text-decoration: underline; text-underline-offset: 3px; }
.history-link span { font-size: 14px; }
.search-hero--results { grid-template-columns: minmax(0, 1fr) 76px; min-height: 82px; gap: 18px; }
.search-hero--results .hero-art { width: 76px; height: 76px; }
.search-hero--results .search-intro { margin-top: 3px; }
.search-hero--results .history-link { margin-top: 4px; }

.hero-art { position: relative; width: 148px; height: 136px; overflow: hidden; isolation: isolate; border: 1px solid var(--hairline); border-radius: var(--panel-radius); background: radial-gradient(ellipse at 80% 20%, color-mix(in oklab, var(--color-accent-500) 12%, transparent), transparent 60%), var(--surface-panel); box-shadow: var(--shadow-panel); transition: width var(--dur-base) var(--ease-out), height var(--dur-base) var(--ease-out); }
.hero-art::before, .hero-art::after { position: absolute; z-index: -1; border: 1px solid color-mix(in oklab, var(--color-neutral-400) 14%, transparent); border-radius: 50%; content: ""; }
.hero-art::before { width: 290px; height: 290px; right: -95px; top: -138px; }
.hero-art::after { width: 224px; height: 224px; right: -61px; top: -105px; }
.hero-art__glow { position: absolute; width: 80px; height: 80px; right: 4%; bottom: -50px; border-radius: 50%; background: color-mix(in oklab, var(--color-accent-500) 22%, transparent); filter: blur(24px); }
.hero-art__sleeve { position: absolute; top: 16%; left: 12%; z-index: 1; width: 66%; aspect-ratio: 1; overflow: hidden; transform: rotate(-7deg); border: 1px solid rgb(255 255 255 / .2); border-radius: 13px; background: linear-gradient(145deg, #e6aa87, #c76c76 52%, #66547e 100%); box-shadow: 0 10px 22px -8px rgb(0 0 0 / .5); }
.hero-art__sleeve-orbit { position: absolute; inset: 12%; border: 1px solid rgb(255 255 255 / .4); border-radius: 50%; }
.hero-art__sleeve-orbit::before, .hero-art__sleeve-orbit::after { position: absolute; inset: 13%; border: 1px solid rgb(255 255 255 / .26); border-radius: 50%; content: ""; }
.hero-art__sleeve-orbit::after { inset: 30%; border-color: rgb(255 255 255 / .5); }
.hero-art__sleeve-sun { position: absolute; top: 19%; right: 18%; width: 28%; aspect-ratio: 1; border-radius: 50%; background: linear-gradient(145deg, #ffd8a3, #fff0c6); box-shadow: 0 6px 26px rgb(255 209 166 / .28); }
.hero-art__record { position: absolute; top: 20%; right: 4%; width: 67%; aspect-ratio: 1; display: grid; place-items: center; border: 1px solid rgb(255 255 255 / .16); border-radius: 50%; background: repeating-radial-gradient(circle at center, #22252b 0 2px, #121419 3px 5px, #252830 6px 7px); box-shadow: 0 12px 24px -10px rgb(0 0 0 / .6); }
.hero-art__record::before { position: absolute; inset: 14%; border: 1px solid rgb(255 255 255 / .1); border-radius: 50%; content: ""; }
.hero-art__record > span { width: 23%; aspect-ratio: 1; border: 3px solid #df9372; border-radius: 50%; background: radial-gradient(circle, #17191e 0 11%, #e6ad82 12% 17%, #cd6a70 18% 100%); }

.search-form { border: 1px solid var(--hairline); border-radius: var(--panel-radius); padding: 12px; background: var(--surface-panel); box-shadow: var(--shadow-panel); }
.search-controls { display: grid; grid-template-columns: minmax(0, 1fr) 104px 124px; gap: 8px; }
.search-input-wrap { position: relative; min-width: 0; }
.search-input-icon { position: absolute; top: 50%; left: 17px; color: var(--color-neutral-500); pointer-events: none; transform: translateY(-50%); transition: color var(--dur-fast) var(--ease-out); }
.search-input-wrap:focus-within .search-input-icon { color: var(--color-accent-300); }
.search-input { width: 100%; min-height: 46px; padding-left: 46px; font-size: 15px; }
.search-input::placeholder { color: var(--color-neutral-500); }
.limit-select { width: 100%; }
.search-submit { min-width: 124px; }
.download-action { color: var(--color-accent-300); }
.download-action:hover:not(:disabled) { border-color: color-mix(in oklab, var(--color-accent-400) 35%, var(--hairline)); background: color-mix(in oklab, var(--color-accent-500) 13%, var(--surface-raised)); }
.history-link:focus-visible, .report-link:focus-visible { outline: 2px solid var(--color-accent-400); outline-offset: 3px; }

.result-summary { min-width: 0; border-radius: var(--panel-radius); padding: 15px 18px; }
.result-summary__main { display: flex; min-width: 0; align-items: flex-start; gap: 11px; }
.result-summary__main p { min-width: 0; margin: 0; color: var(--color-neutral-300); font-size: 12.5px; line-height: 1.7; overflow-wrap: anywhere; }
.result-summary__main strong { color: var(--color-neutral-100); font-weight: 650; }
.registry-version { display: block; min-width: 0; margin-top: 3px; color: var(--color-neutral-500); font-size: 11px; overflow-wrap: anywhere; }
.registry-version code { margin-left: 4px; color: var(--color-neutral-500); font-family: var(--font-mono); font-size: 10.5px; overflow-wrap: anywhere; }
.source-badges { display: flex; flex-wrap: wrap; gap: 7px; margin: 12px 0 0 32px; }
.source-badge { border: 1px solid var(--hairline); border-radius: var(--chip-radius); padding: 5px 10px; color: var(--color-neutral-300); background: var(--surface-raised); box-shadow: var(--shadow-raised); font-size: 10.5px; font-weight: 600; line-height: 1.45; }
.source-badge--mono { font-family: var(--font-mono); font-weight: 500; }
.source-badge--warning { border-color: color-mix(in oklab, var(--color-warning) 35%, transparent); background: color-mix(in oklab, var(--color-warning) 12%, transparent); color: var(--color-warning); }

.results-section { display: grid; gap: 12px; }
.results-head { display: flex; align-items: end; justify-content: space-between; gap: 20px; padding: 6px 1px 2px; }
.results-head h2 { margin: 0; color: var(--color-neutral-50); font-size: 19px; font-weight: 640; letter-spacing: -.02em; }
.results-head h2 span { margin-left: 6px; color: var(--color-neutral-500); font-size: 12px; font-weight: 500; font-variant-numeric: tabular-nums; letter-spacing: 0; }
.results-caption { margin: 6px 0 0; color: var(--color-neutral-500); font-size: 11.5px; line-height: 1.6; }
.result-tools { display: grid; grid-template-columns: repeat(4, minmax(100px, auto)); gap: 7px; }
.result-tools .field-control { min-height: 38px; font-size: 12px; }
.probe-button { min-height: 38px; padding: 0 12px; font-size: 12px; white-space: nowrap; }
.probe-button[aria-pressed="true"] { border-color: color-mix(in oklab, var(--color-accent-400) 45%, var(--hairline)); color: var(--color-accent-200); background: color-mix(in oklab, var(--color-accent-500) 14%, var(--surface-raised)); }
.filtered-empty { border-radius: var(--panel-radius); padding: 24px; color: var(--color-neutral-500); text-align: center; font-size: 12.5px; }
.result-card { border-radius: var(--panel-radius); padding: 13px; transition: border-color var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out), box-shadow var(--dur-base) var(--ease-out); }
.result-card:hover { transform: translateY(-1px); border-color: color-mix(in oklab, var(--color-accent-400) 35%, var(--hairline)); box-shadow: var(--shadow-panel), 0 0 0 1px color-mix(in oklab, var(--color-accent-500) 10%, transparent); }
.result-card__main { display: grid; grid-template-columns: 76px minmax(0, 1fr) 40px; align-items: start; gap: 15px; }
.record-art { position: relative; min-width: 0; aspect-ratio: 1; overflow: hidden; border: 1px solid rgb(255 255 255 / .16); border-radius: var(--card-radius); background: linear-gradient(145deg, #f0bb85, #cd6c70 53%, #593b79); box-shadow: inset 0 1px 0 rgb(255 255 255 / .18), 0 10px 22px -8px rgb(0 0 0 / .45); }
.record-art--ember { background: linear-gradient(145deg, #f0bb85, #cd6c70 53%, #593b79); }
.record-art--tide { background: linear-gradient(145deg, #7dc8c0, #347d91 52%, #37476c); }
.record-art--orchid { background: linear-gradient(145deg, #e5a4c4, #965d9d 53%, #404d7c); }
.record-art::before { position: absolute; inset: 12%; border: 1px solid rgb(255 255 255 / .42); border-radius: 50%; content: ""; }
.record-art::after { position: absolute; inset: 25%; border: 1px solid rgb(255 255 255 / .33); border-radius: 50%; content: ""; }
.record-art__arc { position: absolute; top: -19%; right: -16%; width: 88%; aspect-ratio: 1; border: 1px solid rgb(255 255 255 / .54); border-radius: 50%; }
.record-art__sun { position: absolute; top: 16%; left: 18%; width: 27%; aspect-ratio: 1; border-radius: 50%; background: rgb(255 231 190 / .87); box-shadow: 0 2px 14px rgb(255 231 190 / .35); }
.result-card__copy { min-width: 0; padding-top: 1px; }
.result-card__title-row { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.result-card__title-row h3 { min-width: 0; margin: 0; color: var(--color-neutral-50); font-size: 15px; font-weight: 640; letter-spacing: -.015em; line-height: 1.4; overflow-wrap: anywhere; }
.platform-chip { flex: 0 0 auto; max-width: 100%; overflow: hidden; border: 1px solid color-mix(in oklab, var(--color-accent-500) 24%, transparent); border-radius: var(--chip-radius); padding: 3px 7px; color: var(--color-accent-300); background: color-mix(in oklab, var(--color-accent-500) 11%, transparent); font-family: var(--font-mono); font-size: 9.5px; font-weight: 600; letter-spacing: .04em; line-height: 1.2; text-overflow: ellipsis; text-transform: uppercase; white-space: nowrap; }
.result-card__artist { margin: 4px 0 0; color: var(--color-neutral-300); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.result-card__artist span { margin: 0 3px; color: var(--color-neutral-600); }
.track-metadata { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 8px; color: var(--color-neutral-500); font-size: 11px; line-height: 1.5; }
.track-metadata > span { position: relative; }
.track-metadata > span + span::before { position: absolute; top: 50%; left: -8px; width: 2px; height: 2px; border-radius: 50%; background: var(--color-neutral-600); content: ""; transform: translateY(-50%); }
.source-reference { margin: 7px 0 0; color: var(--color-neutral-500); font-family: var(--font-mono); font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.available-channels { margin: 4px 0 0; color: var(--color-neutral-500); font-size: 11px; line-height: 1.55; overflow-wrap: anywhere; }
.download-action { align-self: start; }
.download-error { margin: 12px 0 0; color: var(--color-danger); font-size: 12px; }
.download-report { display: grid; gap: 5px; margin-top: 13px; border-top: 1px solid var(--hairline); padding-top: 11px; color: var(--color-neutral-400); font-size: 11px; line-height: 1.65; overflow-wrap: anywhere; }
.download-report p { margin: 0; }
.report-link { color: var(--color-accent-300); text-decoration: none; overflow-wrap: anywhere; }
.report-link:hover { text-decoration: underline; }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 10px; border-radius: var(--card-radius); padding: 8px; }
.pagination > span { color: var(--color-neutral-500); font-size: 11px; font-variant-numeric: tabular-nums; text-align: center; }

.no-results > div > p:last-child { margin: 6px 0 0; color: var(--color-neutral-400); font-size: 13px; line-height: 1.7; }
.no-results { display: flex; align-items: center; gap: 15px; border-radius: var(--panel-radius); padding: 21px; }
.no-results__mark { display: grid; width: 46px; height: 46px; flex: 0 0 46px; place-items: center; border: 1px solid color-mix(in oklab, var(--color-accent-400) 22%, transparent); border-radius: var(--card-radius); color: var(--color-accent-300); background: color-mix(in oklab, var(--color-accent-500) 12%, var(--surface-raised)); }
.no-results h2 { margin: 7px 0 0; color: var(--color-neutral-50); font-size: 16px; font-weight: 640; }

@media (max-width: 760px) {
  .search-hero { grid-template-columns: minmax(0, 1fr) 128px; gap: 16px; }
  .hero-art { width: 128px; height: 118px; }
  .search-hero--results { grid-template-columns: minmax(0, 1fr) 76px; }
  .search-hero--results .hero-art { width: 76px; height: 76px; }
  .hero-art__sleeve { left: 8%; width: 62%; }
  .hero-art__record { right: -2%; width: 67%; }
  .results-head { align-items: stretch; flex-direction: column; gap: 13px; }
  .result-tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 980px) {
  .results-head { align-items: stretch; flex-direction: column; gap: 13px; }
  .result-tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .search-hero { grid-template-columns: minmax(0, 1fr) 58px; gap: 10px; min-height: 64px; }
  .search-hero--results { grid-template-columns: minmax(0, 1fr) 58px; }
  .search-hero__copy { padding: 2px 0 0; }
  .search-title { font-size: 30px; }
  .search-intro { margin-top: 3px; font-size: 13px; }
  .history-link { margin-top: 4px; }
  .hero-art { width: 58px; height: 58px; border-radius: 14px; }
  .search-hero--results .hero-art { width: 58px; height: 58px; }
  .hero-art__sleeve { top: 14%; left: 6%; width: 73%; border-radius: 8px; }
  .hero-art__record { top: 20%; right: -20%; width: 80%; }
  .search-form { border-radius: var(--card-radius); padding: 10px; }
  .search-controls { grid-template-columns: minmax(0, 1fr) 96px; gap: 7px; }
  .search-input-wrap { grid-column: 1 / -1; }
  .search-input { min-height: 46px; }
  .search-submit { min-width: 0; }
  .result-summary { padding: 13px; }
  .registry-version { display: block; margin: 3px 0 0; }
  .source-badges { margin-left: 0; }
  .results-head h2 { font-size: 19px; }
  .result-tools { gap: 6px; }
  .result-tools .field-control, .probe-button { min-height: 38px; font-size: 11px; }
  .result-card { border-radius: var(--card-radius); padding: 10px; }
  .result-card__main { grid-template-columns: 58px minmax(0, 1fr) 36px; gap: 9px; }
  .record-art { border-radius: 11px; }
  .result-card__title-row h3 { font-size: 14px; }
  .result-card__artist { font-size: 11px; }
  .track-metadata { gap: 5px 12px; margin-top: 7px; font-size: 11px; }
  .source-reference { margin-top: 6px; font-size: 10px; }
  .download-action { width: 36px; min-height: 36px; }
  .pagination { gap: 5px; padding: 7px; }
  .pagination .action-secondary { min-height: 36px; padding: 0 10px; font-size: 11px; }
  .pagination > span { font-size: 10px; }
  .no-results { align-items: flex-start; gap: 12px; padding: 16px; }
}
@media (pointer: coarse) {
  .result-tools .field-control, .probe-button, .pagination .action-secondary { min-height: 44px; }
  .download-action { width: 44px; min-height: 44px; }
}
</style>
