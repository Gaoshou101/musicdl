import type { ConfigFieldView, ConfigReport } from '@/lib/api'

export type ConfigDraft = string | boolean
export type ConfigDrafts = Record<string, ConfigDraft>

/** A secret's value is intentionally never present in a report. */
export function displayedValue(field: ConfigFieldView): string {
  if (field.value === null || field.value === undefined) return ''
  return Array.isArray(field.value) ? field.value.join(', ') : String(field.value)
}

export function createConfigDrafts(report: ConfigReport): ConfigDrafts {
  const result: ConfigDrafts = {}
  for (const group of report.groups) {
    for (const field of group.fields) {
      result[field.key] = field.kind === 'bool'
        ? Boolean(field.value)
        : field.secret ? '' : displayedValue(field)
    }
  }
  return result
}

export function isConfigFieldDirty(
  field: ConfigFieldView,
  drafts: ConfigDrafts,
  cleared: readonly string[],
): boolean {
  if (cleared.includes(field.key)) return true
  const draft = drafts[field.key]
  if (draft === undefined) return false
  if (field.kind === 'bool') return draft !== Boolean(field.value)
  // A blank write-only secret means "keep the stored value" even when set.
  return draft !== displayedValue(field)
}

export function configPayloadValue(
  field: ConfigFieldView,
  drafts: ConfigDrafts,
  cleared: readonly string[],
): unknown {
  if (cleared.includes(field.key)) return null
  const draft = drafts[field.key]
  if (field.kind === 'bool') return Boolean(draft)
  if (field.kind === 'int') return Math.trunc(Number(draft))
  if (field.kind === 'float') return Number(draft)
  if (field.kind === 'list') {
    return String(draft ?? '').split(',').map((part) => part.trim()).filter(Boolean)
  }
  return String(draft ?? '')
}

export function configDraftProblem(field: ConfigFieldView, drafts: ConfigDrafts): string | null {
  if (field.kind !== 'int' && field.kind !== 'float') return null
  const raw = String(drafts[field.key] ?? '').trim()
  if (!raw) return `「${field.label}」需要一个数字`
  const value = Number(raw)
  if (!Number.isFinite(value)) return `「${field.label}」必须是数字`
  if (field.kind === 'int' && !Number.isInteger(value)) return `「${field.label}」必须是整数`
  return null
}

export function collectConfigChanges(
  report: ConfigReport,
  drafts: ConfigDrafts,
  cleared: readonly string[],
): { values: Record<string, unknown>; problem: string | null } {
  const values: Record<string, unknown> = {}
  for (const group of report.groups) {
    for (const field of group.fields) {
      if (!isConfigFieldDirty(field, drafts, cleared)) continue
      if (!cleared.includes(field.key)) {
        const problem = configDraftProblem(field, drafts)
        if (problem) return { values: {}, problem }
      }
      values[field.key] = configPayloadValue(field, drafts, cleared)
    }
  }
  return { values, problem: null }
}

export function countConfigChanges(
  report: ConfigReport,
  drafts: ConfigDrafts,
  cleared: readonly string[],
): number {
  return report.groups.reduce(
    (total, group) => total + group.fields.filter((field) => isConfigFieldDirty(field, drafts, cleared)).length,
    0,
  )
}

export function configGroupIsDirty(
  report: ConfigReport | null,
  groupId: string,
  drafts: ConfigDrafts,
  cleared: readonly string[],
): boolean {
  return report?.groups.find((group) => group.id === groupId)?.fields.some(
    (field) => isConfigFieldDirty(field, drafts, cleared),
  ) ?? false
}
