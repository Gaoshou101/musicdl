import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import SourcesPage from './SourcesPage.vue'
import type { SourceDiagnosticJob, SourceDiagnosticSnapshot, SourceItem } from '@/lib/api'

const api = vi.hoisted(() => ({
  cancelSourceDiagnosticJob: vi.fn(),
  checkSourceLossless: vi.fn(),
  deleteSource: vi.fn(),
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : '未知错误',
  listSourceHealth: vi.fn(),
  listSources: vi.fn(),
  reloadNote: () => '',
  sourceDiagnostics: vi.fn(),
  startSourceDiagnostic: vi.fn(),
  startSourceDiagnosticBatch: vi.fn(),
  updateSource: vi.fn(),
}))

vi.mock('@/lib/api', () => api)

const source: SourceItem = {
  id: 'demo', enabled: true, priority: 1, timeout: 10, name: 'Demo',
  plugin: {
    sha256: 'abc', version: '1.0.0', language: 'js',
    egress: { allowed_hosts: [], allowed_ports: [443], allow_insecure_http: false, allow_ip_hosts: false, allow_any_host: false },
  },
}
const result = (testedAt: number, overrides: Partial<SourceDiagnosticSnapshot['results'][number]> = {}) => ({
  source_id: 'demo', source_version: '1.0.0', fingerprint: 'abc', status: 'ok' as const,
  stage: 'resolve' as const, message: 'ok', query: 'query', actual_query: 'query', queries_tried: ['query'],
  tested_at: testedAt, search_ms: 20, resolve_ms: 30, extension: 'flac', quality: 'lossless',
  download_verified: false as const, stale: false, ...overrides,
})
const job = (jobId: string, status: SourceDiagnosticJob['status'], submittedAt = 100) => ({
  job_id: jobId, source_id: 'demo', source_version: '1.0.0', fingerprint: 'abc', status,
  stage: status === 'running' ? 'search' : 'queue', query: 'query', submitted_at: submittedAt,
  deadline_at: submittedAt + 60, batch_id: null,
}) satisfies SourceDiagnosticJob
const snapshot = (jobs: SourceDiagnosticJob[] = [], results: SourceDiagnosticSnapshot['results'] = []): SourceDiagnosticSnapshot => ({
  results, jobs, active_limit: 2, pending_limit: 8, active: jobs.filter((entry) => entry.status === 'running').length,
  pending: jobs.filter((entry) => entry.status === 'queued').length,
})
const settle = async () => {
  for (let attempt = 0; attempt < 10; attempt += 1) await Promise.resolve()
  await nextTick()
}
let originalHidden: PropertyDescriptor | undefined

describe('SourcesPage diagnostics', () => {
  beforeEach(() => {
    originalHidden = Object.getOwnPropertyDescriptor(document, 'hidden')
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    vi.clearAllMocks()
    api.listSources.mockResolvedValue({ items: [source] })
    api.listSourceHealth.mockResolvedValue({ sources: [] })
    api.sourceDiagnostics.mockResolvedValue(snapshot())
    api.startSourceDiagnostic.mockResolvedValue({ mode: 'scheduled', source_id: 'demo', job_id: 'job-new', status: 'queued' })
  })
  afterEach(() => {
    vi.useRealTimers()
    if (originalHidden) Object.defineProperty(document, 'hidden', originalHidden)
    else Reflect.deleteProperty(document, 'hidden')
  })

  it('stops diagnostic polling after a running job reaches a terminal result', async () => {
    vi.useFakeTimers()
    api.sourceDiagnostics.mockReset()
      .mockResolvedValueOnce(snapshot([job('job-1', 'running')]))
      .mockResolvedValueOnce(snapshot([job('job-1', 'complete')], [result(Date.now() / 1000)]))
    const wrapper = mount(SourcesPage)
    await settle()
    expect(api.sourceDiagnostics).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1500)
    await settle()

    expect(api.sourceDiagnostics).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('搜索与 FLAC 解析响应正常')
    await vi.advanceTimersByTimeAsync(6000)
    await settle()
    expect(api.sourceDiagnostics).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('restores a terminal diagnostic result without starting a poll loop', async () => {
    vi.useFakeTimers()
    api.sourceDiagnostics.mockResolvedValue(snapshot([], [result(100)]))
    const wrapper = mount(SourcesPage)
    await settle()

    expect(wrapper.text()).toContain('搜索与 FLAC 解析响应正常')
    await vi.advanceTimersByTimeAsync(6000)
    await settle()
    expect(api.sourceDiagnostics).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('keeps a newly scheduled job and hides an older job result with a different job id', async () => {
    vi.useFakeTimers()
    const oldResult = result(100, { actual_query: 'stale-query' })
    api.sourceDiagnostics.mockReset()
      .mockResolvedValueOnce(snapshot([], [oldResult]))
      .mockResolvedValue(snapshot([job('job-old', 'running', 200)], [oldResult]))
    const wrapper = mount(SourcesPage)
    await settle()
    expect(wrapper.text()).toContain('实际查询：stale-query')

    await wrapper.get('[aria-label="诊断 Demo"]').trigger('click')
    await settle()

    expect(api.startSourceDiagnostic).toHaveBeenCalledWith('demo', '')
    expect(wrapper.text()).toContain('等待检查')
    expect(wrapper.text()).toContain('1 项正在进行')
    expect(wrapper.text()).not.toContain('实际查询：stale-query')
    wrapper.unmount()
  })
})
