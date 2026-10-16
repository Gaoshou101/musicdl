import { describe, expect, it } from 'vitest'
import type { SourceDiagnosticResult } from '@/lib/api'
import { currentDiagnosticSnapshotResults } from './diagnosticSnapshot'

const result = (testedAt: number): SourceDiagnosticResult & { stale: boolean } => ({
  source_id: 'demo', source_version: '1', fingerprint: 'sha256', status: 'ok', stage: 'resolve',
  message: 'ok', query: 'query', actual_query: 'query', queries_tried: ['query'], tested_at: testedAt,
  search_ms: 10, resolve_ms: 20, extension: 'flac', quality: 'lossless', download_verified: false, stale: false,
})

describe('currentDiagnosticSnapshotResults', () => {
  it('keeps the newest per-source result and rejects older snapshots and pre-run results', () => {
    const highWater = new Map<string, number>()
    const latestRunAt = new Map<string, number>()
    const first = currentDiagnosticSnapshotResults([result(200), result(300)], latestRunAt, highWater)

    expect(first.map((entry) => entry.tested_at)).toEqual([300])
    expect(currentDiagnosticSnapshotResults([result(250)], latestRunAt, highWater)).toEqual([])
    expect(currentDiagnosticSnapshotResults([result(299)], new Map([['demo', 301]]), highWater)).toEqual([])
  })
})
