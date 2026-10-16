import type { SourceDiagnosticResult, SourceDiagnosticSnapshot } from '@/lib/api'

export function diagnosticResultScope(result: Pick<SourceDiagnosticResult, 'source_id' | 'fingerprint'>): string {
  return result.source_id + '\u0000' + (result.fingerprint ?? '')
}

/** Keep a per-source high-water mark so an older overlapping poll cannot roll back a newer result. */
export function currentDiagnosticSnapshotResults(
  results: SourceDiagnosticSnapshot['results'],
  latestRunAt: ReadonlyMap<string, number>,
  highWater: Map<string, number>,
): Array<SourceDiagnosticResult & { stale: boolean }> {
  for (const result of results) {
    const scope = diagnosticResultScope(result)
    highWater.set(scope, Math.max(highWater.get(scope) ?? -Infinity, result.tested_at))
  }
  return results
    .filter((result) => result.tested_at >= (latestRunAt.get(result.source_id) ?? -Infinity) &&
      result.tested_at >= (highWater.get(diagnosticResultScope(result)) ?? -Infinity))
    .map((result) => ({ ...result, stale: Boolean(result.stale) }))
}
