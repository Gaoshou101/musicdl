/** Pure helpers shared by the URL import dialog and its queue harness. */

import type { ImportPreview, MutationReport, SourceImport, SourceItem } from './api'

export const SOURCE_IMPORT_MAX_URLS = 20
export const SOURCE_IMPORT_MAX_BYTES = 256 * 1024
export const SOURCE_IMPORT_EXTENSIONS = ['.js', '.mjs', '.cjs', '.py'] as const

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

/** Parse one URL per non-empty line, preserving order and bounded queue size. */
export function parseSourceUrlLines(value: string, max = SOURCE_IMPORT_MAX_URLS): string[] {
  const urls = value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  return urls.slice(0, Math.max(0, max))
}

export function hasTooManySourceUrls(value: string, max = SOURCE_IMPORT_MAX_URLS): boolean {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length > max
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
