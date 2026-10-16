import type { Page, ServiceLogEntry, ServiceLogPage } from './api'

export const LOG_CAP = 500
export const SERVICE_POLL_MS = 3000
export const EVENT_POLL_MS = 10000

export async function readTail<T>(fetcher: (offset: number, limit: number, signal?: AbortSignal) => Promise<Page<T>>, limit: number, signal?: AbortSignal): Promise<Page<T>> {
  const head = await fetcher(0, 1, signal)
  if (head.total <= 1) return head
  return fetcher(Math.max(0, head.total - limit), limit, signal)
}

export function mergeServiceLogs(current: ServiceLogEntry[], page: ServiceLogPage, reset: boolean, previousGeneration?: string): ServiceLogEntry[] {
  if (reset || page.reset || (previousGeneration && page.generation && previousGeneration !== page.generation)) return page.items.slice(-LOG_CAP)
  if (!page.items.length) return current
  const seen = new Set(current.map((entry) => entry.id))
  return [...current, ...page.items.filter((entry) => !seen.has(entry.id))].slice(-LOG_CAP)
}

export function visibleEventRows<T extends { status?: string }>(rows: T[], filter: 'all' | 'failed'): T[] {
  return filter === 'all' ? rows : rows.filter((row) => row.status !== 'success')
}

export function clockTime(value: string): string {
  const moment = new Date(value)
  return Number.isNaN(moment.getTime()) ? value : moment.toLocaleTimeString('zh-CN', { hour12: false })
}
