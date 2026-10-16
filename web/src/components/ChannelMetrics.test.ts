import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { SourceHealthRow } from '@/lib/api'
import ChannelMetrics from './ChannelMetrics.vue'
import RecentRequests from './RecentRequests.vue'

const row = {
  id: 'source-a', name: 'Source A', enabled: true, priority: 1, configured: true, status: 'degraded',
  attempts: 7, successes: 3, failures: 2, success_rate: .6, searches: 10, downloads: 9, refreshes: 0,
  last_search: null, last_download: null, last_refresh: null, last_count: 0, last_error: 'search_timeout',
  last_error_stage: 'search', last_health: true, last_health_status: 'ok', lossless: null,
  lossless_status: 'unknown', lossless_stale: false, lossless_checked_at: null, lossless_evidence: {},
  metrics: {
    scope: 'process', window_size: 20,
    search: { samples: 6, successes: 4, failures: 2, rate: 2 / 3 },
    download: { samples: 3, successes: 2, failures: 1, excluded: 2, rate: 2 / 3 },
    quality: { samples: 2, fulfilled: 1, downgraded: 1, unknown: 4, rate: .5 },
    ranking: { eligible: false, minimum_samples: 5, score: 0 },
  },
  recent_requests: Array.from({ length: 12 }, (_, index) => ({ timestamp: 1_700_000_000 + index, kind: index % 2 ? 'download' : 'search', status: index === 11 ? 'excluded' : 'success' })),
} as unknown as SourceHealthRow

describe('channel metric presentation', () => {
  it('keeps each metric denominator and excluded/downgraded evidence visible', () => {
    const wrapper = mount(ChannelMetrics, { props: { row } })
    expect(wrapper.text()).toContain('搜索可用率 67% · 6 次')
    expect(wrapper.text()).toContain('来源尝试成功率 67% · 3 次')
    expect(wrapper.text()).toContain('无损请求兑现率 50% · 可判定 2 次')
    expect(wrapper.text()).toContain('取消或本地原因 2 次')
    expect(wrapper.text()).toContain('降级 1 次')
    expect(wrapper.text()).toContain('音质未知 4 次')
    expect(wrapper.text()).toContain('下载样本不足 5 次，统计不参与选源排序')
    expect(wrapper.findAll('[role="img"]')).toHaveLength(10)
  })

  it('handles nullable rates and unavailable metric support without inventing evidence', () => {
    const noRate = { ...row, metrics: { ...row.metrics!, search: { ...row.metrics!.search, rate: null } } } as SourceHealthRow
    const metrics = mount(ChannelMetrics, { props: { row: noRate } })
    expect(metrics.text()).toContain('搜索可用率 — · 6 次')
    const fallback = mount(ChannelMetrics, { props: { row: { ...row, metrics: undefined } } })
    expect(fallback.text()).toContain('后端尚未提供分项统计')
    expect(fallback.findAll('[role="img"]')).toHaveLength(10)
  })

  it('distinguishes absent recent-request data from a reported empty window', () => {
    expect(mount(RecentRequests, { props: { requests: undefined } }).text()).toContain('后端尚未提供近期请求记录')
    expect(mount(RecentRequests, { props: { requests: [] } }).text()).toContain('暂无近期请求')
  })
})
