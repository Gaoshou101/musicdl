<template>
  <Teleport to="body">
    <div class="fixed inset-0 z-50 overflow-y-auto bg-neutral-950/80 p-3 backdrop-blur-sm sm:p-6" @keydown.esc="close">
      <section role="dialog" aria-modal="true" aria-labelledby="import-title" class="glass glass-highlight mx-auto my-3 w-full max-w-3xl rounded-2xl p-4 sm:my-8 sm:p-6">
        <header class="mb-5 flex items-start justify-between gap-4">
          <div><p class="text-xs font-semibold uppercase tracking-[0.16em] text-accent-300">Source catalog</p><h2 id="import-title" class="mt-1 text-xl font-semibold">导入音源脚本</h2>
            <p class="mt-1.5 text-sm leading-relaxed text-neutral-400">选择本地脚本或粘贴 URL 加入队列。服务会逐个分析身份和出口授权，确认后再安装。</p></div>
          <button type="button" class="icon-action shrink-0" aria-label="关闭导入音源" :disabled="busy && !canCancel" @click="close"><X :size="19" /></button>
        </header>

        <label class="group block cursor-pointer">
          <input type="file" accept=".js,.mjs,.cjs,.py" multiple class="sr-only" :disabled="busy" @change="onFiles">
          <span class="block rounded-xl border border-dashed border-neutral-700 bg-neutral-900/20 p-5 text-center transition-colors group-hover:border-accent-400/50 sm:p-7">
            <UploadSimple :size="32" weight="duotone" class="mx-auto text-accent-300" />
            <span class="mt-2 block font-medium">选择 JS / PY 文件（可多选）</span>
            <span class="mt-1 block text-xs text-neutral-500">支持 lx-music 和普通脚本 · 每个文件不超过 {{ formatBytes(SOURCE_IMPORT_MAX_BYTES) }}</span>
          </span>
        </label>

        <section class="mt-5 border-t border-neutral-800 pt-5">
          <div class="flex items-start gap-3"><LinkSimple :size="20" class="mt-0.5 shrink-0 text-accent-300" />
            <div><h3 class="text-sm font-semibold">从 URL 批量获取</h3>
              <p class="mt-1 text-xs leading-relaxed text-neutral-400">每行一个音源地址，服务会按顺序获取并分析，最多 {{ SOURCE_IMPORT_MAX_URLS }} 项。</p>
              <p class="mt-1 text-xs leading-relaxed text-neutral-500">可粘贴 <a href="https://raw.githubusercontent.com/Gaoshou101/musicdl/main/sources/catalog.json" target="_blank" rel="noreferrer" class="text-accent-300 underline underline-offset-2">聚合音源目录 Raw URL</a>，预览目录内的脚本。</p>
            </div>
          </div>
          <label class="sr-only" for="source-import-urls">音源 URL 列表</label>
          <textarea id="source-import-urls" v-model="urlText" rows="4" :disabled="busy" placeholder="https://example.com/source.js&#10;https://example.org/source.py" class="field-control mt-3 min-h-24 w-full resize-y font-mono text-xs" />
          <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p class="text-xs text-neutral-500">仅支持受安全策略保护的 HTTP(S)，单个脚本不超过 {{ formatBytes(SOURCE_IMPORT_MAX_BYTES) }}。</p>
            <button type="button" class="action-secondary" :disabled="busy || !urlText.trim()" @click="addUrls"><SpinnerGap v-if="busy && canCancel" :size="17" class="animate-spin" /><LinkSimple v-else :size="17" />获取并预览</button>
          </div>
        </section>

        <section v-if="batch.length" aria-label="批量导入队列" class="mt-5 space-y-3 border-t border-neutral-800 pt-5">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div><h3 class="font-semibold">导入队列 <span class="ml-1 text-xs font-normal text-neutral-500">{{ batch.length }} / {{ SOURCE_IMPORT_MAX_URLS }}</span></h3><p class="mt-1 text-xs text-neutral-500">已安装 {{ completedCount }} · 当前可安装 {{ readyCount }}</p></div>
            <button type="button" class="action-primary" :disabled="busy || readyCount === 0" @click="installReady"><SpinnerGap v-if="busy && !canCancel" :size="17" class="animate-spin" /><Check v-else :size="17" weight="bold" />安装可用音源</button>
          </div>
          <article v-for="(row, index) in batch" :key="row.key" class="rounded-xl border border-neutral-800 bg-neutral-900/25 p-3 sm:p-4">
            <div class="flex items-start justify-between gap-3">
              <div class="flex min-w-0 items-start gap-3"><span class="grid size-6 shrink-0 place-items-center rounded-md bg-neutral-800 text-xs text-neutral-400">{{ index + 1 }}</span>
                <div class="min-w-0"><p class="break-words text-sm font-medium">{{ row.filename || '待获取文件名' }}</p><p class="mt-1 break-all font-mono text-[10px] text-neutral-500">{{ row.url }}</p></div>
              </div>
              <div class="flex shrink-0 items-center gap-2"><span class="rounded-md px-2 py-1 text-[11px]" :class="statusClass(row.status)">{{ STATUS_TEXT[row.status] }}</span>
                <button v-if="row.status === 'failed'" type="button" class="action-secondary px-2 py-1 text-xs" :disabled="busy" @click="retry(row)">重试</button>
              </div>
            </div>
            <p v-if="row.error" role="alert" class="mt-3 text-sm text-danger">{{ row.error }}</p>
            <div v-if="row.script || row.status === 'ready' || row.status === 'installed'" class="mt-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_12rem]">
              <label class="text-xs text-neutral-400">音源 ID
                <input :value="row.id" :disabled="rowBusy(row)" :aria-label="'音源 ID ' + (row.filename || row.url)" class="field-control mt-1 w-full font-mono text-sm" @input="setRowId(row, ($event.target as HTMLInputElement).value)" @blur="finishIdEdit(row)">
              </label>
              <label class="text-xs text-neutral-400">脚本语言
                <select :value="row.language" :disabled="rowBusy(row)" class="field-control mt-1 w-full text-sm" @change="editRow(row, { language: ($event.target as HTMLSelectElement).value as SourceLanguage })"><option value="javascript">javascript</option><option value="python">python</option></select>
              </label>
            </div>
            <div v-if="row.preview" class="mt-4 space-y-3 border-t border-neutral-800 pt-3">
              <div class="flex items-start gap-2"><CheckCircle v-if="row.preview.installable" :size="18" weight="fill" class="mt-0.5 shrink-0 text-success" /><WarningCircle v-else :size="18" weight="fill" class="mt-0.5 shrink-0 text-warning" />
                <div class="min-w-0 flex-1"><p class="text-sm font-medium">{{ analysis(row).name || row.id || row.filename || '未命名音源' }}<span v-if="analysis(row).version" class="font-normal text-neutral-400"> v{{ analysis(row).version }}</span></p><p class="mt-1 text-xs text-neutral-400">{{ row.preview.installable ? '可安装' : '还需要处理' }} · {{ row.preview.install_path === 'lx' ? 'lx 自定义音源' : '通用脚本' }}<span v-if="analysis(row).author"> · {{ analysis(row).author }}</span></p></div>
                <span v-if="row.existing" class="rounded-md bg-warning/15 px-2 py-1 text-[10px] text-warning">将替换现有版本</span><span v-if="duplicates.has(row.id.trim())" class="rounded-md bg-danger/15 px-2 py-1 text-[10px] text-danger">ID 重复</span>
              </div>
              <div v-if="analysis(row).description" class="text-xs leading-relaxed text-neutral-400">{{ analysis(row).description }}</div>
              <div v-for="finding in analysis(row).blockers ?? []" :key="finding.code" class="text-xs text-danger">拒绝原因 {{ finding.code }}：{{ finding.detail }}</div>
              <div v-for="finding in analysis(row).caveats ?? []" :key="finding.code" class="text-xs text-neutral-400">提示 {{ finding.code }}：{{ finding.detail }}</div>
              <div v-if="requiredGrants(row).length" class="space-y-2 rounded-lg bg-neutral-900/45 p-3">
                <p class="text-xs text-neutral-400">该脚本请求以下出口权限；只授予你认可的范围：</p>
                <label v-for="grant in requiredGrants(row)" :key="grant" class="flex cursor-pointer items-start gap-2">
                  <input type="checkbox" class="mt-0.5 size-4 accent-accent-500" :checked="grantChecked(row, grant)" :disabled="rowBusy(row)" @change="toggleGrant(row, grant)">
                  <span class="text-xs"><span class="font-medium">{{ GRANT_LABELS[grant]?.title ?? grant }}</span><span class="mt-0.5 block leading-relaxed text-neutral-500">{{ GRANT_LABELS[grant]?.detail }}</span></span>
                </label>
                <p v-if="row.preview.missing_grants.length" class="text-xs text-warning">还缺：{{ row.preview.missing_grants.join('、') }}</p>
              </div>
              <p v-if="analysis(row).sha256" class="break-all font-mono text-[10px] text-neutral-500">SHA-256 {{ analysis(row).sha256 }}<template v-if="analysis(row).size_bytes"> · {{ formatBytes(analysis(row).size_bytes) }}</template></p>
            </div>
            <div v-if="row.reload" role="status" class="mt-3 rounded-lg bg-success/10 px-3 py-2 text-xs text-success">{{ row.reload }}</div>
          </article>
        </section>

        <p v-if="error" role="alert" class="mt-4 rounded-lg border border-danger/20 bg-danger/10 px-3 py-2 text-sm text-danger">{{ error }}</p>
        <footer class="mt-5 flex flex-col-reverse gap-2 border-t border-neutral-800 pt-4 sm:flex-row">
          <button type="button" class="action-secondary flex-1 justify-center" :disabled="busy && !canCancel" @click="close">{{ busy && canCancel ? '取消获取并关闭' : '关闭' }}</button>
          <button type="button" class="action-primary flex-1 justify-center" :disabled="busy || readyCount === 0" @click="installReady"><SpinnerGap v-if="busy && !canCancel" :size="18" class="animate-spin" /><Check v-else :size="18" weight="bold" />{{ busy ? '处理中…' : '安装可用音源' }}</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { PhCheck as Check, PhCheckCircle as CheckCircle, PhLinkSimple as LinkSimple, PhSpinnerGap as SpinnerGap, PhUploadSimple as UploadSimple, PhWarningCircle as WarningCircle, PhX as X } from '@phosphor-icons/vue'
import {
  analyzeSource, errorMessage, fetchSource, fetchSourceInput, installSource, reloadNote,
  type ImportPreview, type SourceImport, type SourceItem,
} from '@/lib/api'
import { formatBytes } from '@/lib/format'
import {
  cloneImportGrants, duplicateSourceIds, hasTooManySourceUrls, importRowIsInstallable,
  installSourceQueueSequentially, NO_IMPORT_GRANTS, parseSourceUrlLines, requiredGrantKeys,
  runSourceImportUrlBatch, sourceImportDraft, SOURCE_IMPORT_MAX_BYTES, SOURCE_IMPORT_MAX_URLS,
  sourceLanguageForFilename, isSupportedSourceFilename,
} from '@/lib/sourceImport'
import type { ImportGrantState, SourceImportRow, SourceLanguage } from '@/lib/sourceImport'

type AnalysisSummary = { name?: string | null; version?: string | null; author?: string | null; homepage?: string | null; description?: string | null; sha256?: string | null; size_bytes?: number | null; blockers?: Array<{ code: string; detail: string }>; caveats?: Array<{ code: string; detail: string }>; allowed_ports?: number[] }
const props = defineProps<{ existingIds: ReadonlySet<string> }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const NO_GRANTS: ImportGrantState = NO_IMPORT_GRANTS
const GRANTS = ['allow_insecure_http', 'allow_ip_hosts', 'allow_any_host', 'allowed_ports'] as const
const GRANT_LABELS: Record<string, { title: string; detail: string }> = {
  allow_insecure_http: { title: '允许明文 http://', detail: '脚本会以未加密的方式请求音频地址，网络流量可能被读取。' },
  allow_ip_hosts: { title: '允许直接使用 IP 地址', detail: '无法通过域名证书校验对端身份。' },
  allow_any_host: { title: '允许访问任意域名', detail: '不再按脚本声明的域名限制出口，范围最大。' },
  allowed_ports: { title: '允许使用非常规端口', detail: '按脚本声明的端口授权，不会开放其他端口。' },
}
const STATUS_TEXT: Record<SourceImportRow['status'], string> = { fetching: '获取中…', analyzing: '分析中…', ready: '待安装', installing: '安装中…', installed: '已安装', failed: '失败' }
const urlText = ref('')
const error = ref('')
const batch = ref<SourceImportRow[]>([])
const busy = ref(false)
const canCancel = ref(false)
const generation = ref(0)
const analysisTokens = new Map<string, number>()
let fetchController: AbortController | null = null
const duplicates = computed(() => duplicateSourceIds(batch.value))
const readyCount = computed(() => batch.value.filter((row) => importRowIsInstallable(row, duplicates.value)).length)
const completedCount = computed(() => batch.value.filter((row) => row.status === 'installed').length)

onBeforeUnmount(() => { generation.value += 1; fetchController?.abort() })
function close(): void {
  if (busy.value && !canCancel.value) return
  generation.value += 1
  fetchController?.abort()
  fetchController = null
  emit('close')
}
function statusClass(status: SourceImportRow['status']): string {
  if (status === 'failed') return 'bg-danger/15 text-danger'
  if (status === 'installed') return 'bg-success/15 text-success'
  if (status === 'ready') return 'bg-accent-500/15 text-accent-300'
  return 'bg-neutral-800 text-neutral-400'
}
function rowBusy(row: SourceImportRow): boolean { return row.status === 'fetching' || row.status === 'analyzing' || row.status === 'installing' || row.status === 'installed' }
function analysis(row: SourceImportRow): AnalysisSummary { return (row.preview?.analysis ?? {}) as AnalysisSummary }
function requiredGrants(row: SourceImportRow): string[] { return requiredGrantKeys(row.preview).filter((grant) => GRANTS.includes(grant as (typeof GRANTS)[number])) }
function updateRow(key: string, update: (row: SourceImportRow) => SourceImportRow): void { batch.value = batch.value.map((row) => row.key === key ? update(row) : row) }
function isCurrent(value: number, controller?: AbortController): boolean { return generation.value === value && !controller?.signal.aborted }

async function analyzeRow(row: SourceImportRow, current = () => true): Promise<void> {
  const token = (analysisTokens.get(row.key) ?? 0) + 1
  analysisTokens.set(row.key, token)
  updateRow(row.key, (value) => ({ ...value, status: 'analyzing', error: null, preview: null }))
  try {
    const draft = sourceImportDraft(row)
    const initial = await analyzeSource(draft)
    if (!current() || analysisTokens.get(row.key) !== token) return
    const id = row.id.trim() || initial.id.suggested
    const preview = !row.id.trim() && id ? await analyzeSource({ ...draft, id }) : initial
    if (!current() || analysisTokens.get(row.key) !== token) return
    updateRow(row.key, (value) => ({ ...value, id, preview, status: 'ready', error: null, existing: props.existingIds.has(id) }))
  } catch (reason) {
    if (!current() || analysisTokens.get(row.key) !== token) return
    updateRow(row.key, (value) => ({ ...value, status: 'failed', error: errorMessage(reason) }))
  }
}
async function analyzeFetched(empty: SourceImportRow, fetched: { script: string; filename: string; language: SourceLanguage }, current = () => true): Promise<void> {
  if (!current()) return
  const row = { ...empty, ...fetched, status: 'analyzing' as const }
  updateRow(empty.key, () => row)
  await analyzeRow(row, current)
}

async function addUrls(): Promise<void> {
  if (busy.value) return
  const remaining = SOURCE_IMPORT_MAX_URLS - batch.value.length
  if (hasTooManySourceUrls(urlText.value, remaining)) { error.value = '导入队列最多保留 20 项，请先安装或清理已有项目。'; return }
  const urls = parseSourceUrlLines(urlText.value)
  if (!urls.length) { error.value = '请先输入音源 URL，每行一个。'; return }
  const run = ++generation.value
  const controller = new AbortController()
  fetchController = controller
  const current = () => isCurrent(run, controller)
  busy.value = true
  canCancel.value = true
  error.value = ''
  const plannedRows: SourceImportRow[] = []
  const status = await runSourceImportUrlBatch(urls, batch.value.length,
    (url) => fetchSourceInput(url, controller.signal),
    async (item, index) => {
      const empty = plannedRows[index]
      if (!empty || !current()) return
      const fetched = item.report ?? await fetchSource(item.url, controller.signal)
      if (!current()) return
      await analyzeFetched(empty, fetched, current)
    }, current,
    (items) => {
      if (!current()) return
      urlText.value = ''
      const rows: SourceImportRow[] = items.map((item, index) => ({
        key: 'url-' + run + '-' + index, url: item.url, topLevelInput: item.topLevelInput, script: '', filename: '',
        language: sourceLanguageForFilename(item.url), id: '', grants: cloneImportGrants(NO_GRANTS), preview: null,
        status: item.error ? 'failed' : 'fetching', error: item.error ? errorMessage(item.error) : null,
        result: null, reload: null, existing: false,
      }))
      plannedRows.push(...rows)
      batch.value = [...batch.value, ...rows]
    },
    (_item, index, reason) => { const row = plannedRows[index]; if (current() && row) updateRow(row.key, (value) => ({ ...value, status: 'failed', error: errorMessage(reason) })) },
  )
  if (generation.value === run) {
    if (status === 'overflow') error.value = '展开后将超过 20 项队列上限；本次没有加入任何项目。'
    busy.value = false
    canCancel.value = false
    fetchController = null
  }
}

async function onFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length || busy.value) return
  if (files.length > SOURCE_IMPORT_MAX_URLS - batch.value.length) { error.value = '导入队列最多保留 20 项，请先安装或清理已有项目。'; return }
  const run = ++generation.value
  busy.value = true
  canCancel.value = true
  error.value = ''
  for (const [index, file] of files.entries()) {
    if (generation.value !== run) break
    const row: SourceImportRow = {
      key: 'file-' + run + '-' + index, url: file.name, file, script: '', filename: file.name,
      language: sourceLanguageForFilename(file.name), id: '', grants: cloneImportGrants(NO_GRANTS), preview: null,
      status: 'fetching', error: null, result: null, reload: null, existing: false,
    }
    batch.value = [...batch.value, row]
    try {
      if (!isSupportedSourceFilename(file.name)) throw new Error('只支持 .js、.mjs、.cjs 和 .py 文件。')
      if (file.size > SOURCE_IMPORT_MAX_BYTES) throw new Error('脚本超过 ' + formatBytes(SOURCE_IMPORT_MAX_BYTES) + ' 限制，请缩小后再导入。')
      const script = await file.text()
      if (generation.value !== run) break
      await analyzeFetched({ ...row, script, filename: file.name, language: sourceLanguageForFilename(file.name) }, { script, filename: file.name, language: sourceLanguageForFilename(file.name) }, () => generation.value === run)
    } catch (reason) {
      if (generation.value !== run) break
      updateRow(row.key, (value) => ({ ...value, status: 'failed', error: reason instanceof Error ? reason.message : errorMessage(reason) }))
    }
  }
  if (generation.value === run) { busy.value = false; canCancel.value = false }
}

function setRowId(row: SourceImportRow, id: string): void {
  updateRow(row.key, (value) => ({ ...value, id, preview: null, error: null, existing: props.existingIds.has(id.trim()) }))
}
function editRow(row: SourceImportRow, changes: Partial<SourceImportRow>): void {
  const next = { ...row, ...changes, preview: null, status: 'analyzing' as const, error: null, result: null, reload: null, existing: props.existingIds.has(String(changes.id ?? row.id).trim()) }
  updateRow(row.key, () => next)
  void analyzeRow(next)
}
function finishIdEdit(row: SourceImportRow): void {
  const current = batch.value.find((value) => value.key === row.key)
  if (current && current.status !== 'installed' && !current.preview && current.script) void analyzeRow(current)
}

async function retry(row: SourceImportRow): Promise<void> {
  if (busy.value || row.status === 'installing') return
  const run = ++generation.value
  const controller = new AbortController()
  fetchController = controller
  const current = () => isCurrent(run, controller)
  busy.value = true
  canCancel.value = true
  error.value = ''
  try {
    if (row.topLevelInput) {
      let replacementRows: SourceImportRow[] = []
      const status = await runSourceImportUrlBatch([row.url], batch.value.length - 1,
        (url) => fetchSourceInput(url, controller.signal),
        async (item, index) => {
          const target = replacementRows[index]
          if (!target || !current()) return
          const fetched = item.report ?? await fetchSource(item.url, controller.signal)
          if (current()) await analyzeFetched(target, fetched, current)
        }, current,
        (items) => {
          if (!current()) return
          replacementRows = items.map((item, index) => {
            const preserve = items.length === 1 && item.topLevelInput && item.url === row.url
            return {
              key: preserve ? row.key : 'retry-url-' + run + '-' + index, url: item.url, topLevelInput: item.topLevelInput,
              script: '', filename: '', language: sourceLanguageForFilename(item.url), id: preserve ? row.id : '',
              grants: cloneImportGrants(preserve ? row.grants : NO_GRANTS), preview: null,
              status: item.error ? 'failed' : 'fetching', error: item.error ? errorMessage(item.error) : null,
              result: null, reload: null, existing: preserve ? row.existing : false,
            } satisfies SourceImportRow
          })
          const index = batch.value.findIndex((item) => item.key === row.key)
          if (index >= 0) batch.value = [...batch.value.slice(0, index), ...replacementRows, ...batch.value.slice(index + 1)]
        },
        (_item, index, reason) => { const target = replacementRows[index]; if (current() && target) updateRow(target.key, (value) => ({ ...value, status: 'failed', error: errorMessage(reason) })) },
      )
      if (status === 'overflow' && current()) error.value = '重试展开后将超过 20 项队列上限；原项目已保留。'
    } else {
      updateRow(row.key, (value) => ({ ...value, status: 'fetching', error: null, preview: null, result: null, reload: null }))
      const fetched = row.file ? { script: await row.file.text(), filename: row.file.name, language: sourceLanguageForFilename(row.file.name) } : await fetchSource(row.url, controller.signal)
      if (current()) await analyzeFetched({ ...row, error: null, preview: null, result: null, reload: null }, fetched, current)
    }
  } catch (reason) {
    if (current()) updateRow(row.key, (value) => ({ ...value, status: 'failed', error: errorMessage(reason) }))
  } finally {
    if (generation.value === run) { busy.value = false; canCancel.value = false; fetchController = null }
  }
}

function toggleGrant(row: SourceImportRow, key: string): void {
  const grants = cloneImportGrants(row.grants)
  if (key === 'allowed_ports') grants.allowed_ports = grants.allowed_ports ? null : (analysis(row).allowed_ports?.length ? analysis(row).allowed_ports! : [443])
  else grants[key as keyof Omit<ImportGrantState, 'allowed_ports'>] = !grants[key as keyof Omit<ImportGrantState, 'allowed_ports'>]
  editRow(row, { grants })
}
function grantChecked(row: SourceImportRow, key: string): boolean { return key === 'allowed_ports' ? Boolean(row.grants.allowed_ports?.length) : Boolean(row.grants[key as keyof Omit<ImportGrantState, 'allowed_ports'>]) }

async function installReady(): Promise<void> {
  if (busy.value) return
  const duplicateIds = duplicates.value
  const ready = batch.value.filter((row) => importRowIsInstallable(row, duplicateIds) && !row.result)
  if (!ready.length) { error.value = duplicateIds.size ? '存在重复的音源 ID，请先修改后再安装。' : '没有可安装的音源，请先完成获取与预览。'; return }
  busy.value = true
  canCancel.value = false
  error.value = ''
  await installSourceQueueSequentially(batch.value, (source) => installSource(source), (row, patch) => {
    updateRow(row.key, (value) => ({ ...value, ...patch, reload: patch.status === 'installed' && patch.result ? reloadNote(patch.result.reload) || null : value.reload }))
    if (patch.status === 'installed') emit('changed')
  }, errorMessage)
  busy.value = false
}
</script>

<style scoped>
.field-control { min-height: 2.65rem; border: 1px solid var(--glass-border); border-radius: .75rem; padding: .55rem .8rem; background: color-mix(in srgb, var(--color-neutral-900) 55%, transparent); color: var(--color-neutral-100); transition: border-color 150ms; }
.field-control:focus { border-color: var(--color-accent-400); outline: none; }
.field-control:disabled { cursor: not-allowed; opacity: .55; }
.field-control::placeholder { color: var(--color-neutral-500); }
.action-primary, .action-secondary { display: inline-flex; align-items: center; justify-content: center; gap: .45rem; min-height: 2.45rem; border-radius: .75rem; padding: .55rem .8rem; font-size: .875rem; font-weight: 600; transition: background-color 150ms, border-color 150ms, opacity 150ms; }
.action-primary { background: var(--color-accent-500); color: var(--color-neutral-100); }
.action-primary:hover:not(:disabled) { background: var(--color-accent-600); }
.action-secondary { border: 1px solid var(--glass-border); background: color-mix(in srgb, var(--color-neutral-900) 45%, transparent); color: var(--color-neutral-200); }
.action-secondary:hover:not(:disabled) { background: var(--color-neutral-800); }
.action-primary:disabled, .action-secondary:disabled { cursor: not-allowed; opacity: .5; }
</style>
