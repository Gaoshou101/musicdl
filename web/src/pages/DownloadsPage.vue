<template>
  <section class="admin-page downloads-page mx-auto w-full max-w-7xl space-y-5 px-4 py-5 sm:px-6 sm:py-7 lg:px-8">
    <header class="page-header flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 class="page-title">下载历史</h1>
        <p class="page-description">查看下载结果、请求音质与换源过程。记录保存在服务端，刷新页面或重启后仍可查看。</p>
        <p class="page-meta">最近 1,000 条已结束记录 · 列表每 5 秒更新</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <RouterLink to="/dashboard/search" class="action-secondary">搜索音乐</RouterLink>
        <button type="button" class="action-secondary" @click="refreshAll"><ArrowClockwise :size="16" />刷新列表</button>
      </div>
    </header>

    <p v-if="error" role="alert" class="rounded-xl border border-danger/20 bg-danger/10 px-4 py-3 text-sm text-danger">{{ error }}</p>

    <section aria-label="下载并发" class="capacity-panel glass">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="font-semibold">下载并发</h2>
          <p class="mt-1 text-xs text-neutral-500">当前服务进程的瞬时占用，更新间隔 3 秒。</p>
        </div>
        <button type="button" aria-label="刷新下载并发" class="action-secondary" @click="refreshObservability(true)"><ArrowClockwise :size="16" />刷新状态</button>
      </div>
      <p v-if="observabilityError" role="alert" class="mt-3 text-sm text-danger">{{ observabilityError }}</p>
      <p v-else-if="!observability" role="status" class="mt-3 text-sm text-neutral-400">正在读取瞬时状态…</p>
      <template v-else-if="observability.admission">
        <dl class="capacity-grid mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div class="capacity-stat"><dt>占用名额</dt><dd>{{ observability.admission.active }} / {{ observability.admission.active_limit }}</dd></div>
          <div class="capacity-stat"><dt>等待下载</dt><dd>{{ observability.admission.pending }} / {{ observability.admission.pending_limit }}</dd></div>
          <div class="capacity-stat"><dt>等待切换音源</dt><dd>{{ observability.admission.source_pending }}</dd></div>
          <div class="capacity-stat"><dt>并发状态</dt><dd :class="observability.admission.active_limit_reached ? 'text-warning' : 'text-success'">{{ observability.admission.active_limit_reached ? '名额已满' : '名额未满' }}</dd></div>
        </dl>
        <p v-if="observability.admission.possible_wait_factors.length" class="mt-3 text-xs text-neutral-400">整体等待可能与：{{ observability.admission.possible_wait_factors.map((factor) => WAIT_LABELS[factor] ?? factor).join('；') }}有关。</p>
        <p class="mt-2 text-xs text-neutral-500">此处显示整体占用，等待条件不对应某一首歌；单曲阶段见下方详情。</p>
      </template>
      <p v-else class="mt-3 text-sm text-neutral-400">当前服务暂未提供并发计数。</p>
    </section>

    <div class="grid items-start gap-6" :class="selected ? 'xl:grid-cols-2' : ''">
      <section aria-label="下载记录" class="min-w-0">
        <p v-if="loading" role="status" class="p-6 text-sm text-neutral-400">正在读取下载记录…</p>
        <div v-else-if="history?.total === 0" class="glass rounded-2xl p-10 text-center">
          <ClockCounterClockwise :size="38" class="mx-auto text-neutral-500" />
          <h2 class="mt-3 font-semibold">还没有下载记录</h2>
          <p class="mt-2 text-sm text-neutral-400">从面板或企业微信下载歌曲后，可以在这里查看结果。</p>
          <RouterLink to="/dashboard/search" class="mt-5 inline-block text-sm text-accent-300 hover:underline">开始搜索音乐 →</RouterLink>
        </div>
        <template v-else-if="history?.items.length">
          <div class="history-caption"><span>共 {{ history.total }} 条记录</span><span>按开始时间从新到旧</span></div>
          <ul class="space-y-3">
            <li v-for="item in history.items" :key="item.id">
              <button type="button" :aria-pressed="selected === item.id" :aria-label="'查看 ' + (item.title || '未命名歌曲') + ' 的下载记录'"
                class="history-row w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-400"
                :class="selected === item.id ? 'history-row--selected' : ''" @click="choose(item.id)">
                <span class="flex items-start justify-between gap-3">
                  <span class="min-w-0"><span class="block truncate font-medium">{{ item.title || '未命名歌曲' }}</span><span class="mt-1 block truncate text-sm text-neutral-400">{{ item.artist || '未知艺术家' }}</span></span>
                  <StatusBadge :status="item.status" />
                </span>
                <span class="mt-3 block text-xs text-neutral-500">{{ dateTime(item.created_at) }} · {{ item.origin === 'wecom' ? '企业微信' : '管理面板' }}</span>
                <span class="mt-1 block break-all text-xs text-neutral-400">{{ item.result?.source_id || item.source_id }} · 请求 {{ item.requested_quality || '未指定' }} · 实际 {{ item.actual_quality || '未确认' }}</span>
                <span v-if="item.error_code" class="mt-2 block text-xs text-warning">{{ downloadErrorMessage(item.error_code) }}</span>
              </button>
            </li>
          </ul>
          <nav aria-label="下载历史分页" class="mt-5 flex items-center justify-between gap-3">
            <button type="button" class="action-secondary" :disabled="offset === 0 || loading" @click="changePage(Math.max(0, offset - PAGE_SIZE))"><ArrowLeft :size="16" />上一页</button>
            <span class="text-xs text-neutral-500">第 {{ Math.floor(offset / PAGE_SIZE) + 1 }} / {{ Math.max(1, Math.ceil(history.total / PAGE_SIZE)) }} 页</span>
            <button type="button" class="action-secondary" :disabled="offset + PAGE_SIZE >= history.total || loading" @click="changePage(offset + PAGE_SIZE)">下一页<ArrowRight :size="16" /></button>
          </nav>
        </template>
      </section>
      <div v-if="selected" ref="detailsRef" class="order-first min-w-0 xl:order-last"><DownloadDetail :key="selected" :id="selected" /></div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { PhArrowClockwise as ArrowClockwise, PhArrowLeft as ArrowLeft, PhArrowRight as ArrowRight, PhClockCounterClockwise as ClockCounterClockwise } from '@phosphor-icons/vue'
import {
  downloadErrorMessage, errorMessage, listDownloadHistory, readDownloadHistory, readDownloadObservability,
  type DownloadHistoryDetail as Detail, type DownloadHistoryEvent, type DownloadHistoryStatus,
  type DownloadHistoryPage, type DownloadObservability,
} from '@/lib/api'
import { formatBytes } from '@/lib/format'
import { usePolling } from '@/composables/usePolling'
import { mediaUrl } from '@/lib/api'

const PAGE_SIZE = 20
const WAIT_LABELS: Record<string, string> = {
  active_limit_reached: '并发名额已满', source_or_user_limit: '音源或用户限额可能影响等待', source_transition_limit: '目标音源名额可能影响切换',
}
const route = useRoute()
const router = useRouter()
const history = ref<DownloadHistoryPage | null>(null)
const observability = ref<DownloadObservability | null>(null)
const offset = ref(0)
const selected = ref(typeof route.query.id === 'string' ? route.query.id : null)
const error = ref('')
const observabilityError = ref('')
const loading = ref(true)
const detailsRef = ref<HTMLElement | null>(null)
const pageRequest = async (signal: AbortSignal): Promise<void> => {
  try {
    const result = await listDownloadHistory(offset.value, PAGE_SIZE, signal)
    if (signal.aborted) return
    if (offset.value > 0 && !result.items.length) {
      offset.value = Math.max(0, Math.ceil(result.total / PAGE_SIZE) - 1) * PAGE_SIZE
      return
    }
    history.value = result
    error.value = ''
  } catch (reason) {
    if (!signal.aborted) error.value = errorMessage(reason)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
const refreshHistory = usePolling(pageRequest, 5000)
const refreshObservability = usePolling(async (signal) => {
  try {
    const result = await readDownloadObservability(signal)
    if (signal.aborted) return
    observability.value = result
    observabilityError.value = ''
  } catch (reason) {
    if (!signal.aborted) {
      observability.value = null
      observabilityError.value = errorMessage(reason)
    }
  }
}, 3000)

watch(offset, () => {
  loading.value = true
  history.value = null
  void refreshHistory(true)
})
watch(() => route.query.id, (id) => { selected.value = typeof id === 'string' ? id : null })
watch(selected, (id) => {
  if (id && typeof window !== 'undefined' && window.matchMedia('(max-width: 1279px)').matches) {
    window.setTimeout(() => detailsRef.value?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 0)
  }
})
function refreshAll(): void { void refreshHistory(true); void refreshObservability(true) }
function changePage(next: number): void { if (next !== offset.value) offset.value = next }
function choose(id: string): void {
  selected.value = id
  void router.replace({ query: { ...route.query, id } })
}
function dateTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

const STATUS: Record<DownloadHistoryStatus, { label: string; style: string }> = {
  running: { label: '下载中', style: 'text-accent-300 bg-accent-500/10' },
  succeeded: { label: '已完成', style: 'text-success bg-success/10' },
  failed: { label: '失败', style: 'text-danger bg-danger/10' },
  interrupted: { label: '已中断', style: 'text-warning bg-warning/10' },
}
const StatusBadge = defineComponent({
  props: { status: { type: String, required: true } },
  setup(props) { return () => { const item = STATUS[props.status as DownloadHistoryStatus] ?? { label: '状态未知', style: 'text-neutral-400 bg-neutral-800' }; return h('span', { class: 'rounded-md px-2 py-1 text-xs font-medium whitespace-nowrap ' + item.style }, item.label) } },
})

const STAGES: Record<string, string> = {
  resolve: '解析音频地址', transfer: '下载与校验', download_attempt: '尝试下载', download: '下载文件',
  channel_switch: '切换音源', quality: '检查音质', quality_downgrade: '音质降级', quality_downgraded: '音质降级',
  duration: '校验时长', integrity: '校验完整性', cleanup: '清理临时文件', refresh: '刷新候选',
  health: '检查音源', task: '下载任务', recovery: '恢复记录',
}
const EVENT_STATUS: Record<string, string> = {
  started: '开始', running: '进行中', success: '成功', succeeded: '完成', failed: '失败', interrupted: '中断',
  cancelled: '已取消', skipped: '跳过', warning: '提示', selected: '已选择', observed: '已记录', unverified: '未验证',
}
const ERROR_ADVICE: Record<string, string> = {
  download_queue_full: '当前等待名额已满，稍后重试。', download_queue_timeout: '等待并发名额超时，稍后重试或减少同时发起的下载。',
  download_source_capacity: '目标音源名额暂不可用，稍后重试或尝试其他音源。', download_source_timeout: '等待目标音源名额超时，稍后重试或尝试其他音源。',
  download_admission_blocked: '下载并发容量暂不可用，稍后重试。', download_admission_timeout: '等待下载并发容量超时，稍后重试。',
  media_timeout: '音源响应超时；可稍后重试，并在音源状态页检查来源。', source_unavailable: '可在音源状态页检查来源，再尝试其他音源。',
  incomplete_audio: '本次音源返回的文件不完整，可尝试其他音源。', service_restarted: '服务重启后无法确认最终结果；重新下载前先检查媒体目录。',
  download_uncertain: '重新下载前先检查媒体目录，避免重复文件。',
}
function elapsed(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return '未记录'
  if (value < 1000) return Math.round(value) + ' 毫秒'
  if (value < 60000) return (value / 1000).toFixed(1) + ' 秒'
  return Math.floor(value / 60000) + ' 分 ' + Math.floor((value % 60000) / 1000) + ' 秒'
}
function reasonText(value: string): string {
  if (value === 'quality_downgrade') return '当前音源未能提供请求的音质'
  if (value.startsWith('content_error:')) return downloadErrorMessage(value.slice('content_error:'.length))
  return downloadErrorMessage(value)
}
function currentPhase(events: DownloadHistoryEvent[]) {
  const open = new Map<string, { event: DownloadHistoryEvent; index: number }>()
  events.forEach((event, index) => {
    if (event.stage !== 'resolve' && event.stage !== 'transfer') return
    const key = [event.stage, event.source_id ?? '', event.candidate_id ?? ''].join('\u0000')
    if (event.status === 'started') open.set(key, { event, index })
    else if (['success', 'failed', 'cancelled', 'interrupted'].includes(event.status ?? '')) open.delete(key)
  })
  const active = [...open.values()].sort((a, b) => a.index - b.index).at(-1)?.event
  const latest = events.at(-1) ?? null
  if (active) return { phase: active.stage ?? 'between_steps', latest }
  if (!latest) return { phase: 'preparing', latest }
  if (latest.stage === 'channel_switch' && latest.status === 'selected') return { phase: 'channel_switch', latest }
  return { phase: 'between_steps', latest }
}
function phaseLabel(phase: string): string {
  if (phase === 'preparing') return '尚无阶段记录'
  if (phase === 'between_steps') return '等待状态更新'
  if (phase === 'channel_switch') return '已选择替代音源'
  if (phase === 'resolve') return '正在解析音源'
  if (phase === 'transfer') return '正在接收并校验音频'
  return STAGES[phase] ?? phase
}
function phaseAdvice(phase: string, latest: DownloadHistoryEvent | null): string {
  if (latest?.error_code) return '最近事件记录了错误“' + downloadErrorMessage(latest.error_code) + '”；任务仍标记为进行中，请等待后续事件确认结果。'
  if (latest?.reason === 'quality_downgrade') return '最近事件显示当前音源未能提供请求音质；系统可能继续尝试其他音源。'
  if (latest?.reason === 'content_failure') return '最近事件显示来源内容校验失败；任务仍标记为进行中，请等待后续尝试结果。'
  if (phase === 'resolve') return '等待音源解析结果；如长时间无更新，可检查音源状态。'
  if (phase === 'transfer') return '耗时取决于文件大小和音源响应。'
  if (phase === 'channel_switch') return '已选择替代音源，正在继续尝试。'
  return '等待后续状态记录。'
}
function dateTimeInner(value: string | null): string { return dateTime(value) }

const DownloadDetail = defineComponent({
  props: { id: { type: String, required: true } },
  setup(props) {
    const detail = ref<Detail | null>(null)
    const detailError = ref('')
    const detailLoading = ref(true)
    const pollEnabled = computed(() => detailLoading.value || detail.value?.status === 'running')
    const loadDetail = async (signal: AbortSignal): Promise<void> => {
      try {
        const result = await readDownloadHistory(props.id, signal)
        if (signal.aborted) return
        detail.value = result
        detailError.value = ''
      } catch (reason) {
        if (!signal.aborted) detailError.value = errorMessage(reason)
      } finally {
        if (!signal.aborted) detailLoading.value = false
      }
    }
    const refresh = usePolling(loadDetail, 3000, pollEnabled)
    return () => {
      const report = detail.value
      const phase = report ? currentPhase(report.events) : null
      const query = report?.query || (report ? (report.title + ' ' + report.artist).trim() : '')
      return h('section', { 'aria-label': '下载详情', class: 'glass min-w-0 scroll-mt-6 rounded-2xl border border-neutral-800 p-4 sm:p-5' }, [
        h('div', { class: 'mb-4 flex items-center justify-between gap-3' }, [h('h2', { class: 'font-semibold' }, '下载详情'), h('button', { type: 'button', 'aria-label': '刷新下载详情', class: 'action-secondary', onClick: () => void refresh(true) }, '刷新')]),
        detailError.value ? h('p', { role: 'alert', class: 'mb-4 text-sm text-danger' }, detailError.value) : null,
        detailLoading.value ? h('p', { role: 'status', class: 'text-sm text-neutral-400' }, '正在读取下载详情…') : null,
        report ? h('div', [
          h('div', { class: 'flex items-start justify-between gap-3' }, [h('div', { class: 'min-w-0' }, [h('h3', { class: 'break-words text-lg font-semibold' }, report.title || '未命名歌曲'), h('p', { class: 'mt-1 break-words text-sm text-neutral-400' }, report.artist || '未知艺术家')]), h(StatusBadge, { status: report.status })]),
          report.status === 'interrupted' ? h('p', { class: 'mt-4 text-sm text-warning' }, '这次执行或记录已中断，最终结果未确认。重新下载前，请先检查媒体目录。') : null,
          report.status === 'running' && phase ? h('div', { class: 'mt-4 rounded-lg border border-neutral-800 bg-neutral-900/40 p-3' }, [h('p', { role: 'status', class: 'text-sm text-neutral-200' }, '最近阶段：' + phaseLabel(phase.phase)), h('p', { class: 'mt-1 text-xs text-neutral-400' }, phaseAdvice(phase.phase, phase.latest)), h('p', { class: 'mt-1 text-xs text-neutral-500' }, '按最近记录更新，不代表实时进度。')]) : null,
          report.error_code ? h('div', { class: 'mt-3' }, [h('p', { class: 'text-sm text-warning' }, downloadErrorMessage(report.error_code)), ERROR_ADVICE[report.error_code] ? h('p', { class: 'mt-1 text-xs text-neutral-400' }, ERROR_ADVICE[report.error_code]) : null]) : null,
          report.history_warning ? h('p', { class: 'mt-3 text-sm text-warning' }, '历史记录保存不完整，部分过程可能缺失。') : null,
          h('dl', { class: 'mt-5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm' }, [
            h('dt', { class: 'text-neutral-500' }, '入口'), h('dd', report.origin === 'wecom' ? '企业微信' : '管理面板'),
            h('dt', { class: 'text-neutral-500' }, '开始时间'), h('dd', dateTimeInner(report.created_at)),
            h('dt', { class: 'text-neutral-500' }, report.status === 'interrupted' ? '状态确认时间' : '结束时间'), h('dd', dateTimeInner(report.finished_at)),
            h('dt', { class: 'text-neutral-500' }, '总耗时'), h('dd', elapsed(report.elapsed_ms)),
            h('dt', { class: 'text-neutral-500' }, '请求音质'), h('dd', report.requested_quality || '未指定'),
            h('dt', { class: 'text-neutral-500' }, '实际音质'), h('dd', report.actual_quality || '未确认'),
            h('dt', { class: 'text-neutral-500' }, report.result ? '交付音源' : '初始音源'), h('dd', { class: 'break-all' }, report.result?.source_id || report.source_id),
          ]),
          report.result ? h('div', { class: 'mt-5 rounded-lg border border-neutral-800 bg-neutral-900/50 p-3 text-sm' }, [h('p', { class: 'text-neutral-400' }, formatBytes(report.result.size_bytes) + ' · ' + (report.result.media_type || '格式未知')), report.result.relative_path ? h('a', { href: mediaUrl(report.result.relative_path), target: '_blank', rel: 'noreferrer', class: 'mt-2 block break-words text-accent-300 hover:underline' }, report.result.relative_path) : null, h('p', { class: 'mt-2 text-xs text-neutral-500' }, '保存位置为下载完成时的记录，文件之后可能已被移动或删除。')]) : null,
          h('p', { class: 'mt-5' }, [h(RouterLink, { to: { path: '/dashboard/search', query: { q: query } }, class: 'text-sm text-accent-300 hover:underline' }, () => '重新搜索这首歌 →')]),
          h('h3', { class: 'mb-3 mt-7 font-medium' }, '下载过程'),
          report.events.length ? h('ol', { class: 'space-y-2' }, report.events.map((event, index) => h('li', { key: index, class: 'break-words border-l-2 border-neutral-700 py-2 pl-4' }, [
            h('div', { class: 'flex flex-wrap items-baseline justify-between gap-2 text-sm' }, [h('span', { class: 'font-medium' }, [STAGES[event.stage ?? ''] ?? event.stage ?? '事件', ' · ', h('span', { class: event.status === 'failed' ? 'text-danger' : 'text-neutral-300' }, EVENT_STATUS[event.status ?? ''] ?? event.status ?? '已记录')]), h('time', { class: 'text-xs text-neutral-500', datetime: event.created_at }, dateTimeInner(event.created_at))]),
            h('p', { class: 'mt-1 break-all text-xs text-neutral-400' }, [(event.from_source_id && event.to_source_id) ? event.from_source_id + ' → ' + event.to_source_id : event.source_id || '—', event.elapsed_ms !== null ? ' · 耗时 ' + elapsed(event.elapsed_ms) : '']),
            event.requested_quality || event.actual_quality ? h('p', { class: 'mt-1 text-xs text-neutral-400' }, [(event.requested_quality ? '请求 ' + event.requested_quality : ''), (event.requested_quality && event.actual_quality) ? ' · ' : '', (event.actual_quality ? '实际 ' + event.actual_quality : '')]) : null,
            event.error_code ? h('p', { class: 'mt-1 text-sm text-warning' }, downloadErrorMessage(event.error_code)) : null,
            event.reason ? h('p', { class: 'mt-1 text-xs text-neutral-400' }, reasonText(event.reason)) : null,
          ]))) : h('p', { class: 'text-sm text-neutral-500' }, '暂未记录到来源尝试。'),
          report.events_truncated ? h('p', { class: 'mt-3 text-xs text-warning' }, '过程记录已达到保留上限，部分事件未展示。') : null,
          h('p', { class: 'mt-5 break-all text-xs text-neutral-500' }, '记录编号：' + report.id),
        ]) : null,
      ])
    }
  },
})
</script>

<style scoped>
.downloads-page { color: var(--color-neutral-100); font-size: 14px; }
.page-title { margin: 0; color: var(--color-neutral-50); font-size: clamp(28px, 3vw, 34px); font-weight: 620; letter-spacing: -.045em; line-height: 1.2; }
.page-description { max-width: 45rem; margin: 7px 0 0; color: var(--color-neutral-400); font-size: 14px; line-height: 1.65; }
.page-meta { margin: 7px 0 0; color: var(--color-neutral-500); font-size: 12px; line-height: 1.55; }
.capacity-panel { border-radius: 18px; padding: 16px; box-shadow: none; background: color-mix(in srgb, var(--glass-bg) 72%, var(--color-neutral-950)); }
.capacity-panel h2 { color: var(--color-neutral-100); font-size: 16px; font-weight: 620; }
.capacity-grid { gap: 8px; }
.capacity-stat { min-width: 0; border-left: 2px solid var(--glass-border); padding: 2px 10px; }
.capacity-stat dt { color: var(--color-neutral-500); font-size: 12px; line-height: 1.45; }
.capacity-stat dd { margin: 5px 0 0; color: var(--color-neutral-100); font-size: 15px; font-weight: 620; font-variant-numeric: tabular-nums; line-height: 1.35; }
.history-caption { display: flex; justify-content: space-between; gap: 12px; margin: 0 1px 9px; color: var(--color-neutral-500); font-size: 12px; }
.history-row { display: block; min-width: 0; border: 1px solid var(--glass-border); border-radius: 14px; padding: 14px; background: color-mix(in srgb, var(--glass-bg) 58%, var(--color-neutral-950)); transition: border-color 140ms, background-color 140ms; }
.history-row:hover { border-color: color-mix(in srgb, var(--color-neutral-500) 42%, var(--glass-border)); }
.history-row--selected { border-color: color-mix(in srgb, var(--color-accent-500) 60%, var(--glass-border)); background: color-mix(in srgb, var(--color-accent-500) 8%, var(--glass-bg)); }
.history-row > span:nth-of-type(2) { color: var(--color-neutral-500); font-size: 12px; line-height: 1.55; }
.history-row > span:nth-of-type(3) { font-size: 12px; line-height: 1.55; }
.downloads-page nav { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.downloads-page nav > span { color: var(--color-neutral-500); font-size: 12px; }
.downloads-page :deep(section[aria-label="下载详情"]) { border-radius: 18px; border-color: var(--glass-border); padding: 16px; box-shadow: none; }
.downloads-page :deep(section[aria-label="下载详情"] h2) { font-size: 16px; font-weight: 620; }
.downloads-page :deep(section[aria-label="下载详情"] dl) { gap: 8px 14px; font-size: 12px; }
.downloads-page :deep(section[aria-label="下载详情"] dd) { min-width: 0; color: var(--color-neutral-200); overflow-wrap: anywhere; }
.downloads-page :deep(section[aria-label="下载详情"] ol li) { border-color: var(--glass-border); font-size: 12px; }
.action-primary, .action-secondary { display: inline-flex; align-items: center; justify-content: center; gap: .5rem; min-height: 44px; border-radius: 12px; padding: 9px 13px; font-size: 12px; font-weight: 580; transition: background-color 150ms, border-color 150ms, opacity 150ms; }
.action-primary { background: var(--color-accent-500); color: var(--color-neutral-50); }
.action-primary:hover:not(:disabled) { background: var(--color-accent-600); }
.action-secondary { border: 1px solid var(--glass-border); background: color-mix(in srgb, var(--color-neutral-900) 45%, transparent); color: var(--color-neutral-200); }
.action-secondary:hover:not(:disabled) { background: var(--color-neutral-800); }
.action-primary:disabled, .action-secondary:disabled { cursor: not-allowed; opacity: .5; }
@media (max-width: 560px) {
  .capacity-panel { padding: 14px; }
  .capacity-stat { padding-right: 4px; padding-left: 8px; }
  .capacity-stat dd { font-size: 14px; }
  .history-caption { flex-wrap: wrap; font-size: 11px; }
  .history-row { padding: 12px; }
  .downloads-page nav > button { min-height: 40px; padding: 8px 10px; font-size: 11px; }
}
</style>
