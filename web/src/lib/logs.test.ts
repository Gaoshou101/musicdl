import { describe, expect, it } from 'vitest'
import type { ServiceLogEntry, ServiceLogPage } from './api'
import { LOG_CAP, mergeServiceLogs, readTail, visibleEventRows } from './logs'

const row = (id: number): ServiceLogEntry => ({ id, time: '2026-01-01T00:00:00Z', level: 'INFO', logger: 'test', message: `line ${id}`, traceback: null })
const page = (items: ServiceLogEntry[], extra: Partial<ServiceLogPage> = {}): ServiceLogPage => ({ items, total: items.length, last_id: items.at(-1)?.id ?? 0, dropped: 0, ...extra })

describe('log windows and filters', () => {
  it('loads the newest tail from an offset-backed page', async () => {
    const calls: [number, number][] = []
    const result = await readTail(async (offset, limit) => {
      calls.push([offset, limit])
      return offset === 0 && limit === 1 ? { items: [], total: 250, offset, limit } : { items: [], total: 250, offset, limit }
    }, 100)
    expect(calls).toEqual([[0, 1], [150, 100]])
    expect(result.offset).toBe(150)
  })

  it('resets on a server generation change, even when IDs are reused and reset is omitted', () => {
    const merged = mergeServiceLogs([row(40), row(41)], page([row(1)], { generation: 'new' }), false, 'old')
    expect(merged.map((entry) => entry.id)).toEqual([1])
  })

  it('replaces on an explicit reset, de-duplicates repeated IDs, and caps the window', () => {
    expect(mergeServiceLogs([row(1)], page([row(1), row(2)]), false).map((entry) => entry.id)).toEqual([1, 2])
    expect(mergeServiceLogs([row(99)], page([], { reset: true }), false)).toEqual([])
    expect(mergeServiceLogs([], page(Array.from({ length: LOG_CAP + 10 }, (_, i) => row(i))), false)).toHaveLength(LOG_CAP)
  })

  it('keeps successful rows only in all mode and includes all non-success statuses in failed mode', () => {
    const rows = [{ status: 'success' }, { status: 'failed' }, { status: 'unavailable' }, { status: 'rate_limited' }]
    expect(visibleEventRows(rows, 'all')).toHaveLength(4)
    expect(visibleEventRows(rows, 'failed')).toEqual(rows.slice(1))
  })
})
