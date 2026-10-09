/** Pure helpers shared by the URL import dialog and its queue harness. */

import type {
  ImportPreview,
  MutationReport,
  SourceFetchInputReport,
  SourceFetchReport,
  SourceImport,
  SourceItem,
} from './api'

export const SOURCE_IMPORT_MAX_URLS = 20
export const SOURCE_IMPORT_MAX_BYTES = 256 * 1024
export const SOURCE_IMPORT_EXTENSIONS = ['.js', '.mjs', '.cjs', '.py'] as const
export const SOURCE_LIST_FORMAT = 'musicdl-source-list/v1'

export type SourceLanguage = 'javascript' | 'python'

export type ImportGrantState = {
  allow_insecure_http: boolean
  allow_ip_hosts: boolean
  allow_any_host: boolean
  allowed_ports: number[] | null
}

export const NO_IMPORT_GRANTS: ImportGrantState = {
  allow_insecure_http: false,
  allow_ip_hosts: false,
  allow_any_host: false,
  allowed_ports: null,
}

export type SourceImportStatus =
  | 'fetching'
  | 'analyzing'
  | 'ready'
  | 'installing'
  | 'installed'
  | 'failed'

export type SourceImportRow = {
  key: string
  url: string
  /** Top-level inputs opt in to source-list expansion when retried. */
  topLevelInput?: boolean
  file?: File
  script: string
  filename: string
  language: SourceLanguage
  id: string
  grants: ImportGrantState
  preview: ImportPreview | null
  status: SourceImportStatus
  error: string | null
  result: MutationReport<SourceItem> | null
  reload: string | null
  existing: boolean
}

/** Parse every URL line in order; queue capacity is checked before appending. */
export function parseSourceUrlLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

export function hasTooManySourceUrls(value: string, max = SOURCE_IMPORT_MAX_URLS): boolean {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length > max
}

export type SourceImportPlanItem = {
  url: string
  /** True only for the user's top-level URL; children must be script-only. */
  topLevelInput: boolean
  report: SourceFetchReport | null
  error: unknown | null
}

export type SourceImportUrlBatchStatus = 'complete' | 'cancelled' | 'overflow'

function isSourceListReport(value: SourceFetchInputReport): value is Extract<SourceFetchInputReport, { kind: 'source_list' }> {
  return typeof value === 'object' && value !== null && 'kind' in value && value.kind === 'source_list'
}

function isSourceFetchReport(value: unknown): value is SourceFetchReport {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<SourceFetchReport>
  return typeof candidate.script === 'string' && typeof candidate.filename === 'string' &&
    (candidate.language === 'javascript' || candidate.language === 'python')
}

function sourceListUrls(value: Extract<SourceFetchInputReport, { kind: 'source_list' }>): string[] {
  if (value.format !== SOURCE_LIST_FORMAT || !Array.isArray(value.sources)) {
    throw new Error('音源目录格式不受支持。')
  }
  const urls = value.sources.map((source) => {
    if (!source || typeof source.url !== 'string' || !source.url.trim()) {
      throw new Error('音源目录包含无效 URL。')
    }
    return source.url.trim()
  })
  if (!urls.length) throw new Error('音源目录没有可导入的项目。')
  return urls
}

/**
 * Resolve top-level URLs sequentially. Direct scripts are kept in the plan;
 * list members are represented as script-only children and are not fetched yet.
 */
export async function planSourceImportUrls(
  urls: ReadonlyArray<string>,
  fetchInput: (url: string) => Promise<SourceFetchInputReport>,
  isCurrent: () => boolean = () => true,
): Promise<{ status: 'complete' | 'cancelled'; items: SourceImportPlanItem[] }> {
  const items: SourceImportPlanItem[] = []
  for (const url of urls) {
    if (!isCurrent()) return { status: 'cancelled', items: [] }
    try {
      const fetched = await fetchInput(url)
      if (!isCurrent()) return { status: 'cancelled', items: [] }
      if (isSourceListReport(fetched)) {
        for (const childUrl of sourceListUrls(fetched)) {
          items.push({ url: childUrl, topLevelInput: false, report: null, error: null })
        }
      } else if (isSourceFetchReport(fetched)) {
        items.push({ url, topLevelInput: true, report: fetched, error: null })
      } else {
        throw new Error('音源获取服务返回了无效结果。')
      }
    } catch (error) {
      if (!isCurrent()) return { status: 'cancelled', items: [] }
      // A failed top-level URL remains visible and consumes one queue slot.
      items.push({ url, topLevelInput: true, report: null, error })
    }
  }
  return isCurrent() ? { status: 'complete', items } : { status: 'cancelled', items: [] }
}

/**
 * Plan, reserve queue capacity atomically, then fetch/analyze each child in
 * order. Processing failures are isolated so later children still run.
 */
export async function runSourceImportUrlBatch<T>(
  urls: ReadonlyArray<string>,
  existingCount: number,
  fetchInput: (url: string) => Promise<SourceFetchInputReport>,
  process: (item: SourceImportPlanItem, index: number) => Promise<T>,
  isCurrent: () => boolean,
  onPlanned: (items: ReadonlyArray<SourceImportPlanItem>) => void,
  onFailed: (item: SourceImportPlanItem, index: number, error: unknown) => void,
): Promise<SourceImportUrlBatchStatus> {
  const planned = await planSourceImportUrls(urls, fetchInput, isCurrent)
  if (planned.status === 'cancelled' || !isCurrent()) return 'cancelled'
  if (existingCount + planned.items.length > SOURCE_IMPORT_MAX_URLS) return 'overflow'

  onPlanned(planned.items)
  for (const [index, item] of planned.items.entries()) {
    if (!isCurrent()) return 'cancelled'
    if (item.error) {
      onFailed(item, index, item.error)
      continue
    }
    try {
      await process(item, index)
    } catch (error) {
      if (!isCurrent()) return 'cancelled'
      onFailed(item, index, error)
    }
  }
  return isCurrent() ? 'complete' : 'cancelled'
}

export function isSupportedSourceFilename(filename: string): boolean {
  const lower = filename.trim().toLocaleLowerCase()
  return SOURCE_IMPORT_EXTENSIONS.some((suffix) => lower.endsWith(suffix))
}

export function sourceLanguageForFilename(filename: string): SourceLanguage {
  return filename.toLocaleLowerCase().endsWith('.py') ? 'python' : 'javascript'
}

export function cloneImportGrants(grants: ImportGrantState): ImportGrantState {
  return { ...grants, allowed_ports: grants.allowed_ports ? [...grants.allowed_ports] : null }
}

export function sourceImportDraft(row: Pick<SourceImportRow, 'id' | 'script' | 'filename' | 'language' | 'grants'>): SourceImport {
  return {
    id: row.id.trim(),
    script: row.script,
    filename: row.filename || undefined,
    language: row.language,
    allow_insecure_http: row.grants.allow_insecure_http || undefined,
    allow_ip_hosts: row.grants.allow_ip_hosts || undefined,
    allow_any_host: row.grants.allow_any_host || undefined,
    allowed_ports: row.grants.allowed_ports ?? undefined,
  }
}

export function requiredGrantKeys(preview: ImportPreview | null): string[] {
  return preview ? Object.keys(preview.required_grants ?? {}) : []
}

export function duplicateSourceIds(rows: ReadonlyArray<Pick<SourceImportRow, 'id'>>): Set<string> {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const id = row.id.trim()
    if (id) counts.set(id, (counts.get(id) ?? 0) + 1)
  }
  return new Set([...counts].filter(([, count]) => count > 1).map(([id]) => id))
}

export function importRowIsInstallable(
  row: Pick<SourceImportRow, 'id' | 'script' | 'preview' | 'status'>,
  duplicateIds: ReadonlySet<string>,
): boolean {
  return Boolean(row.id.trim() && row.script.trim() && row.preview?.installable &&
    row.status === 'ready' && !duplicateIds.has(row.id.trim()))
}

export function queueInstallableRows(rows: ReadonlyArray<SourceImportRow>): SourceImportRow[] {
  const duplicates = duplicateSourceIds(rows)
  return rows.filter((row) => importRowIsInstallable(row, duplicates))
}

export type SourceInstallRowPatch = Partial<Pick<SourceImportRow, 'status' | 'error' | 'result' | 'reload'>>

export type SourceInstallQueueReport = {
  attempted: number
  installed: number
  failed: number
}

/**
 * Install the same ready rows the dialog presents, one at a time.
 *
 * The callback owns React state (and any success notice), while this helper
 * owns ordering, duplicate filtering, and the important partial-success rule:
 * a failed row returns to ``ready`` so a later call retries it, and successful
 * rows are skipped because they now carry a result.
 */
export async function installSourceQueueSequentially(
  rows: ReadonlyArray<SourceImportRow>,
  install: (source: SourceImport) => Promise<SourceItem>,
  update: (row: SourceImportRow, patch: SourceInstallRowPatch) => void,
  formatError: (error: unknown) => string,
): Promise<SourceInstallQueueReport> {
  const ready = queueInstallableRows(rows).filter((row) => !row.result)
  let installed = 0
  let failed = 0
  for (const row of ready) {
    update(row, { status: 'installing', error: null })
    try {
      const result = await install(sourceImportDraft(row))
      update(row, { status: 'installed', result, error: null })
      installed += 1
    } catch (error) {
      update(row, { status: 'ready', error: formatError(error) })
      failed += 1
    }
  }
  return { attempted: ready.length, installed, failed }
}
