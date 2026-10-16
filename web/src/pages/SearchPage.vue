<template>
  <section class="music-search-page mx-auto w-full max-w-6xl space-y-6 px-4 py-6 sm:space-y-7 sm:px-6 sm:py-8 lg:px-8">
    <header class="search-hero">
      <div class="search-hero__copy">
        <h1 class="search-title">音乐搜索</h1>
        <p class="search-intro">搜索歌曲与艺术家，找到可下载的音乐。</p>
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

    <form class="search-form glass" @submit.prevent="runSearch(query)">
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

    <section v-if="!report && !error && !searching" class="empty-state glass">
      <div class="empty-state__art" aria-hidden="true"><span></span><i></i></div>
      <div class="empty-state__copy">
        <h2>开始搜索音乐</h2>
        <p>输入歌曲或艺术家名称，浏览可用音源与音质信息。</p>
      </div>
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
.search-hero { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(280px, .9fr); align-items: center; gap: clamp(28px, 5vw, 68px); }
.search-hero__copy { max-width: 39rem; padding: 9px 0; }
.search-title { margin: 0; color: var(--color-neutral-50); font-size: clamp(42px, 6vw, 68px); font-weight: 750; letter-spacing: -.075em; line-height: .98; }
.search-intro { max-width: 34rem; margin: 16px 0 0; color: var(--color-neutral-400); font-size: 14px; line-height: 1.8; }
.history-link { display: inline-flex; align-items: center; gap: 8px; margin-top: 22px; color: var(--color-accent-300); font-size: 12px; font-weight: 650; text-decoration: none; transition: color 140ms, gap 140ms; }
.history-link:hover { gap: 12px; color: var(--color-accent-200); }
.history-link span { font-size: 15px; }

.hero-art { position: relative; min-height: 246px; overflow: hidden; isolation: isolate; border: 1px solid var(--glass-border); border-radius: 24px; background: radial-gradient(ellipse at 72% 25%, color-mix(in srgb, var(--color-accent-500) 20%, transparent), transparent 54%), linear-gradient(135deg, color-mix(in srgb, var(--color-neutral-900) 74%, var(--glass-bg)), var(--glass-bg)); box-shadow: var(--glass-shadow); }
.hero-art::before, .hero-art::after { position: absolute; z-index: -1; border: 1px solid color-mix(in srgb, var(--color-neutral-400) 14%, transparent); border-radius: 50%; content: ""; }
.hero-art::before { width: 290px; height: 290px; right: -95px; top: -138px; }
.hero-art::after { width: 224px; height: 224px; right: -61px; top: -105px; }
.hero-art__glow { position: absolute; width: 180px; height: 180px; right: 10%; bottom: -110px; border-radius: 50%; background: color-mix(in srgb, var(--color-accent-500) 21%, transparent); filter: blur(40px); }
.hero-art__sleeve { position: absolute; top: 12%; left: 14%; z-index: 1; width: 54%; aspect-ratio: 1; overflow: hidden; transform: rotate(-8deg); border: 1px solid rgb(255 255 255 / .18); border-radius: 17px; background: linear-gradient(145deg, #f4b183, #d85d70 48%, #613b88 100%); box-shadow: 0 20px 38px rgb(0 0 0 / .24); }
.hero-art__sleeve-orbit { position: absolute; inset: 12%; border: 1px solid rgb(255 255 255 / .4); border-radius: 50%; }
.hero-art__sleeve-orbit::before, .hero-art__sleeve-orbit::after { position: absolute; inset: 13%; border: 1px solid rgb(255 255 255 / .26); border-radius: 50%; content: ""; }
.hero-art__sleeve-orbit::after { inset: 30%; border-color: rgb(255 255 255 / .5); }
.hero-art__sleeve-sun { position: absolute; top: 19%; right: 18%; width: 28%; aspect-ratio: 1; border-radius: 50%; background: linear-gradient(145deg, #ffd8a3, #fff0c6); box-shadow: 0 6px 26px rgb(255 209 166 / .28); }
.hero-art__record { position: absolute; top: 16%; right: 7%; width: 55%; aspect-ratio: 1; display: grid; place-items: center; border: 1px solid rgb(255 255 255 / .16); border-radius: 50%; background: repeating-radial-gradient(circle at center, #22252b 0 2px, #121419 3px 5px, #252830 6px 7px); box-shadow: 0 18px 34px rgb(0 0 0 / .42); }
.hero-art__record::before { position: absolute; inset: 14%; border: 1px solid rgb(255 255 255 / .1); border-radius: 50%; content: ""; }
.hero-art__record > span { width: 23%; aspect-ratio: 1; border: 3px solid #df9372; border-radius: 50%; background: radial-gradient(circle, #17191e 0 11%, #e6ad82 12% 17%, #cd6a70 18% 100%); }

.search-form { border-radius: 21px; padding: 19px; box-shadow: var(--glass-shadow); }
.search-controls { display: grid; grid-template-columns: minmax(0, 1fr) 112px 124px; gap: 10px; }
.search-input-wrap { position: relative; min-width: 0; }
.search-input-icon { position: absolute; top: 50%; left: 17px; color: var(--color-neutral-500); pointer-events: none; transform: translateY(-50%); }
.field-control { min-width: 0; min-height: 46px; border: 1px solid var(--glass-border); border-radius: 12px; padding: 10px 13px; background: color-mix(in srgb, var(--color-neutral-950) 28%, var(--glass-bg)); color: var(--color-neutral-100); font-size: 13px; transition: border-color 150ms, background-color 150ms, box-shadow 150ms; }
.field-control:focus { border-color: var(--color-accent-400); outline: none; box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-accent-400) 16%, transparent); }
.field-control:disabled { cursor: not-allowed; opacity: .55; }
.field-control option { background: var(--color-neutral-900); color: var(--color-neutral-100); }
.search-input { width: 100%; min-height: 58px; padding-left: 48px; font-size: 16px; }
.search-input::placeholder { color: var(--color-neutral-500); }
.limit-select { width: 100%; }
.action-primary, .action-secondary { display: inline-flex; min-height: 44px; align-items: center; justify-content: center; gap: 8px; border-radius: 12px; padding: 10px 15px; font-size: 12px; font-weight: 700; transition: background-color 150ms, border-color 150ms, opacity 150ms, transform 150ms; }
.action-primary { border: 1px solid transparent; background: var(--color-accent-500); color: var(--color-neutral-50); }
.action-primary:hover:not(:disabled) { background: var(--color-accent-600); transform: translateY(-1px); }
.action-secondary { border: 1px solid var(--glass-border); background: color-mix(in srgb, var(--color-neutral-900) 40%, var(--glass-bg)); color: var(--color-neutral-200); }
.action-secondary:hover:not(:disabled) { background: var(--color-neutral-800); }
.action-primary:disabled, .action-secondary:disabled { cursor: not-allowed; opacity: .5; }
.search-submit { min-width: 124px; }
.icon-action { display: inline-grid; min-width: 42px; min-height: 42px; place-items: center; border: 1px solid var(--glass-border); border-radius: 13px; background: color-mix(in srgb, var(--color-neutral-900) 38%, var(--glass-bg)); color: var(--color-accent-300); transition: background-color 150ms, transform 150ms; }
.icon-action:hover:not(:disabled) { background: color-mix(in srgb, var(--color-accent-500) 13%, var(--glass-bg)); transform: translateY(-1px); }
.icon-action:disabled { cursor: not-allowed; opacity: .5; }
.history-link:focus-visible, .action-primary:focus-visible, .action-secondary:focus-visible, .icon-action:focus-visible, .report-link:focus-visible { outline: 2px solid var(--color-accent-400); outline-offset: 3px; }

.inline-message { border: 1px solid; border-radius: 13px; padding: 12px 15px; font-size: 12px; line-height: 1.55; }
.inline-message--danger { border-color: color-mix(in srgb, var(--color-danger) 25%, transparent); background: color-mix(in srgb, var(--color-danger) 8%, transparent); color: var(--color-danger); }
.inline-message--warning { border-color: color-mix(in srgb, var(--color-warning) 25%, transparent); background: color-mix(in srgb, var(--color-warning) 8%, transparent); color: var(--color-warning); }
.result-summary { min-width: 0; border-radius: 17px; padding: 15px 17px; }
.result-summary__main { display: flex; min-width: 0; align-items: flex-start; gap: 11px; }
.result-summary__main p { min-width: 0; margin: 0; color: var(--color-neutral-300); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.result-summary__main strong { color: var(--color-neutral-100); font-weight: 650; }
.registry-version { min-width: 0; margin-left: 7px; color: var(--color-neutral-500); overflow-wrap: anywhere; }
.registry-version code { margin-left: 4px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.source-badges { display: flex; flex-wrap: wrap; gap: 7px; margin: 12px 0 0 32px; }
.source-badge { border: 1px solid var(--glass-border); border-radius: 8px; padding: 5px 8px; color: var(--color-neutral-300); background: color-mix(in srgb, var(--color-neutral-900) 35%, var(--glass-bg)); font-size: 10px; line-height: 1.45; }
.source-badge--mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.source-badge--warning { border-color: color-mix(in srgb, var(--color-warning) 22%, transparent); background: color-mix(in srgb, var(--color-warning) 9%, transparent); color: var(--color-warning); }

.results-section { display: grid; gap: 12px; }
.results-head { display: flex; align-items: end; justify-content: space-between; gap: 20px; padding: 6px 1px 2px; }
.results-head h2 { margin: 8px 0 0; color: var(--color-neutral-50); font-size: 21px; font-weight: 700; letter-spacing: -.04em; }
.results-head h2 span { margin-left: 5px; color: var(--color-neutral-500); font-size: 12px; font-weight: 500; letter-spacing: 0; }
.results-caption { margin: 6px 0 0; color: var(--color-neutral-500); font-size: 11px; line-height: 1.6; }
.result-tools { display: grid; grid-template-columns: repeat(4, minmax(100px, auto)); gap: 7px; }
.result-tools .field-control { min-height: 38px; padding: 7px 9px; font-size: 11px; }
.probe-button { min-height: 38px; padding: 7px 10px; font-size: 11px; white-space: nowrap; }
.filtered-empty { border-radius: 16px; padding: 24px; color: var(--color-neutral-500); text-align: center; font-size: 12px; }
.result-card { border-radius: 19px; padding: 14px; box-shadow: var(--glass-shadow); transition: border-color 160ms, transform 160ms; }
.result-card:hover { border-color: color-mix(in srgb, var(--color-accent-400) 28%, var(--glass-border)); transform: translateY(-1px); }
.result-card__main { display: grid; grid-template-columns: 76px minmax(0, 1fr) 42px; align-items: start; gap: 15px; }
.record-art { position: relative; min-width: 0; aspect-ratio: 1; overflow: hidden; border: 1px solid rgb(255 255 255 / .15); border-radius: 14px; background: linear-gradient(145deg, #f0bb85, #cd6c70 53%, #593b79); box-shadow: 0 8px 19px rgb(0 0 0 / .18); }
.record-art--ember { background: linear-gradient(145deg, #f0bb85, #cd6c70 53%, #593b79); }
.record-art--tide { background: linear-gradient(145deg, #7dc8c0, #347d91 52%, #37476c); }
.record-art--orchid { background: linear-gradient(145deg, #e5a4c4, #965d9d 53%, #404d7c); }
.record-art::before { position: absolute; inset: 12%; border: 1px solid rgb(255 255 255 / .42); border-radius: 50%; content: ""; }
.record-art::after { position: absolute; inset: 25%; border: 1px solid rgb(255 255 255 / .33); border-radius: 50%; content: ""; }
.record-art__arc { position: absolute; top: -19%; right: -16%; width: 88%; aspect-ratio: 1; border: 1px solid rgb(255 255 255 / .54); border-radius: 50%; }
.record-art__sun { position: absolute; top: 16%; left: 18%; width: 27%; aspect-ratio: 1; border-radius: 50%; background: rgb(255 231 190 / .87); box-shadow: 0 2px 14px rgb(255 231 190 / .35); }
.result-card__copy { min-width: 0; padding-top: 1px; }
.result-card__title-row { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.result-card__title-row h3 { min-width: 0; margin: 0; color: var(--color-neutral-50); font-size: 16px; font-weight: 700; letter-spacing: -.025em; line-height: 1.4; overflow-wrap: anywhere; }
.platform-chip { flex: 0 0 auto; max-width: 100%; overflow: hidden; border: 1px solid color-mix(in srgb, var(--color-accent-500) 20%, transparent); border-radius: 6px; padding: 3px 6px; color: var(--color-accent-300); background: color-mix(in srgb, var(--color-accent-500) 10%, transparent); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 9px; line-height: 1.2; text-overflow: ellipsis; text-transform: uppercase; white-space: nowrap; }
.result-card__artist { margin: 4px 0 0; color: var(--color-neutral-300); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.result-card__artist span { margin: 0 3px; color: var(--color-neutral-600); }
.track-metadata { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 9px; color: var(--color-neutral-500); font-size: 10px; line-height: 1.5; }
.track-metadata > span { position: relative; }
.track-metadata > span + span::before { position: absolute; top: 50%; left: -8px; width: 2px; height: 2px; border-radius: 50%; background: var(--color-neutral-600); content: ""; transform: translateY(-50%); }
.source-reference { margin: 8px 0 0; color: var(--color-neutral-500); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 9px; line-height: 1.6; overflow-wrap: anywhere; }
.available-channels { margin: 4px 0 0; color: var(--color-neutral-500); font-size: 10px; line-height: 1.55; overflow-wrap: anywhere; }
.download-action { align-self: start; }
.download-error { margin: 12px 0 0; color: var(--color-danger); font-size: 12px; }
.download-report { display: grid; gap: 5px; margin-top: 13px; border-top: 1px solid var(--glass-border); padding-top: 11px; color: var(--color-neutral-400); font-size: 10px; line-height: 1.65; overflow-wrap: anywhere; }
.download-report p { margin: 0; }
.report-link { color: var(--color-accent-300); text-decoration: none; overflow-wrap: anywhere; }
.report-link:hover { text-decoration: underline; }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 10px; border-radius: 15px; padding: 8px; }
.pagination > span { color: var(--color-neutral-500); font-size: 10px; text-align: center; }

.empty-state { display: grid; grid-template-columns: minmax(170px, .65fr) minmax(0, 1fr); align-items: center; gap: clamp(22px, 5vw, 58px); overflow: hidden; border-radius: 22px; padding: clamp(22px, 4vw, 42px); }
.empty-state__art { position: relative; display: grid; width: min(100%, 210px); aspect-ratio: 1; place-items: center; justify-self: center; border: 1px solid color-mix(in srgb, var(--color-accent-400) 18%, var(--glass-border)); border-radius: 50%; background: radial-gradient(circle at 35% 30%, color-mix(in srgb, var(--color-accent-400) 18%, transparent), transparent 57%), repeating-radial-gradient(circle at center, color-mix(in srgb, var(--color-neutral-500) 12%, transparent) 0 1px, transparent 2px 10px); }
.empty-state__art::before, .empty-state__art::after { position: absolute; border: 1px solid color-mix(in srgb, var(--color-accent-400) 30%, transparent); border-radius: 50%; content: ""; }
.empty-state__art::before { inset: 13%; }
.empty-state__art::after { inset: 27%; }
.empty-state__art > span { z-index: 1; width: 20%; aspect-ratio: 1; border-radius: 50%; background: linear-gradient(135deg, var(--color-accent-300), var(--color-accent-600)); box-shadow: 0 0 28px color-mix(in srgb, var(--color-accent-400) 33%, transparent); }
.empty-state__art > i { position: absolute; right: 15%; bottom: 19%; width: 35%; height: 2px; border-radius: 2px; background: var(--color-accent-300); transform: rotate(-40deg); }
.empty-state__copy { max-width: 35rem; }
.empty-state__copy h2 { margin: 0; color: var(--color-neutral-50); font-size: clamp(22px, 3.5vw, 34px); font-weight: 700; letter-spacing: -.055em; line-height: 1.24; }
.empty-state__copy > p:last-child, .no-results > div > p:last-child { margin: 10px 0 0; color: var(--color-neutral-400); font-size: 12px; line-height: 1.8; }
.no-results { display: flex; align-items: center; gap: 15px; border-radius: 18px; padding: 21px; }
.no-results__mark { display: grid; width: 46px; height: 46px; flex: 0 0 46px; place-items: center; border: 1px solid var(--glass-border); border-radius: 14px; color: var(--color-accent-300); background: color-mix(in srgb, var(--color-accent-500) 9%, var(--glass-bg)); }
.no-results h2 { margin: 7px 0 0; color: var(--color-neutral-50); font-size: 16px; font-weight: 700; }

@media (max-width: 760px) {
  .search-hero { grid-template-columns: minmax(0, 1fr) minmax(210px, .72fr); gap: 18px; }
  .hero-art { min-height: 210px; }
  .hero-art__sleeve { left: 8%; width: 58%; }
  .hero-art__record { right: -2%; width: 62%; }
  .results-head { align-items: stretch; flex-direction: column; gap: 13px; }
  .result-tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .search-hero { grid-template-columns: minmax(0, 1fr); gap: 17px; }
  .search-hero__copy { padding: 2px 0 0; }
  .search-title { font-size: clamp(42px, 14vw, 58px); }
  .search-intro { margin-top: 12px; font-size: 13px; }
  .history-link { margin-top: 15px; }
  .hero-art { min-height: 162px; border-radius: 18px; }
  .hero-art__sleeve { top: 9%; left: 20%; width: 39%; border-radius: 13px; }
  .hero-art__record { top: 9%; right: 19%; width: 40%; }
  .search-form { border-radius: 17px; padding: 14px; }
  .search-controls { grid-template-columns: minmax(0, 1fr) 112px; gap: 8px; }
  .search-input-wrap { grid-column: 1 / -1; }
  .search-input { min-height: 54px; }
  .search-submit { min-width: 0; }
  .result-summary { padding: 13px; }
  .registry-version { display: block; margin: 3px 0 0; }
  .source-badges { margin-left: 0; }
  .results-head h2 { font-size: 19px; }
  .result-tools { gap: 6px; }
  .result-tools .field-control, .probe-button { min-height: 40px; font-size: 10px; }
  .result-card { border-radius: 16px; padding: 11px; }
  .result-card__main { grid-template-columns: 62px minmax(0, 1fr) 38px; gap: 10px; }
  .record-art { border-radius: 11px; }
  .result-card__title-row h3 { font-size: 14px; }
  .result-card__artist { font-size: 11px; }
  .track-metadata { gap: 5px 12px; margin-top: 7px; font-size: 9px; }
  .source-reference { margin-top: 6px; font-size: 8px; }
  .download-action { min-width: 36px; min-height: 36px; border-radius: 11px; }
  .pagination { gap: 5px; padding: 7px; }
  .pagination .action-secondary { min-height: 38px; padding: 8px 10px; font-size: 10px; }
  .pagination > span { font-size: 9px; }
  .empty-state { grid-template-columns: minmax(0, 1fr); gap: 18px; border-radius: 18px; padding: 22px 18px; }
  .empty-state__art { width: 112px; }
  .empty-state__copy h2 { margin-top: 9px; font-size: 24px; }
  .empty-state__copy > p:last-child { font-size: 11px; }
  .no-results { align-items: flex-start; gap: 12px; padding: 16px; }
}
</style>
