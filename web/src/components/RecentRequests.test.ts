import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { RecentRequest } from '@/lib/api'
import RecentRequests from './RecentRequests.vue'

describe('RecentRequests', () => {
  it('shows at most the newest ten request outcomes with status-specific evidence', () => {
    const requests: RecentRequest[] = Array.from({ length: 13 }, (_, index) => ({
      timestamp: 1_700_000_000 + index,
      kind: index % 2 ? 'download' : 'search',
      status: index === 12 ? 'excluded' : index === 11 ? 'failure' : 'success',
    }))
    const wrapper = mount(RecentRequests, { props: { requests } })
    const bars = wrapper.findAll('[role="img"]')
    expect(bars).toHaveLength(10)
    expect(bars[0]!.attributes('aria-label')).toContain('下载成功')
    expect(bars.at(-2)!.attributes('aria-label')).toContain('下载失败')
    expect(bars.at(-1)!.attributes('aria-label')).toContain('搜索取消或本地原因，不计入成功率')
    expect(bars.at(-1)!.attributes('style')).toContain('dashed')
    expect(wrapper.text()).toContain('最近请求')
  })

  it('distinguishes an unavailable request window from an empty request window', () => {
    expect(mount(RecentRequests, { props: { requests: undefined } }).text()).toContain('后端尚未提供近期请求记录')
    expect(mount(RecentRequests, { props: { requests: [] } }).text()).toContain('暂无近期请求')
  })
})
