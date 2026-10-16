import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const api = vi.hoisted(() => ({
  errorMessage: vi.fn((error: unknown) => error instanceof Error ? error.message : 'error'),
  mediaUrl: vi.fn((value: string) => value),
  readEvents: vi.fn(),
  readAudit: vi.fn(),
  readServiceLogs: vi.fn(),
}))
vi.mock('@/lib/api', () => api)
import LogsPage from './LogsPage.vue'

const listPage = (items: unknown[] = []) => ({ items, total: items.length, offset: 0, limit: 100 })
const servicePage = (id: number, message: string, generation: string) => ({
  items: [{ id, time: '2026-10-16T00:00:00Z', level: 'INFO', logger: 'musicdl', message, traceback: null }],
  total: 1, last_id: id, dropped: 0, generation,
})
const eventRow = (id: number, status = 'success') => ({
  request_id: `request-${id}`, source_id: `source-${id}`, stage: 'download', status,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function offsetPage<T>(offset: number, limit: number, total: number, makeItem: (index: number) => T) {
  return {
    items: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, index) => makeItem(offset + index)),
    total, offset, limit,
  }
}

describe('LogsPage refresh controls', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1 })
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    api.readEvents.mockImplementation(async (_offset: number, _limit: number) => listPage())
    api.readAudit.mockImplementation(async (_offset: number, _limit: number) => listPage())
    api.readServiceLogs.mockImplementation(async () => ({ items: [], total: 0, last_id: 0, dropped: 0, generation: 'g1' }))
  })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  async function openServiceTab() {
    const wrapper = mount(LogsPage)
    await flushPromises()
    await wrapper.findAll('[role="tab"]')[2]!.trigger('click')
    await flushPromises()
    return wrapper
  }

  it('passes a cursor, then replaces reused IDs after a server generation change', async () => {
    api.readServiceLogs
      .mockResolvedValueOnce(servicePage(7, 'old process line', 'generation-a'))
      .mockResolvedValueOnce(servicePage(7, 'new process line', 'generation-b'))
    const wrapper = await openServiceTab()
    expect(api.readServiceLogs.mock.calls[0]![1]).toBe(0)
    expect(wrapper.text()).toContain('old process line')

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(api.readServiceLogs.mock.calls[1]![1]).toBe(7)
    expect(wrapper.text()).toContain('new process line')
    expect(wrapper.text()).not.toContain('old process line')
    wrapper.unmount()
  })

  it('resets the cursor when the level changes and polls immediately after resuming', async () => {
    const wrapper = await openServiceTab()
    expect(api.readServiceLogs).toHaveBeenCalledTimes(1)
    await wrapper.get('[aria-label="日志级别"]').setValue('warning')
    await flushPromises()
    expect(api.readServiceLogs.mock.calls.at(-1)?.slice(0, 3)).toEqual([200, 0, 'warning'])

    const callsBeforePause = api.readServiceLogs.mock.calls.length
    await wrapper.findAll('button').find((button) => button.text().includes('暂停'))!.trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.readServiceLogs).toHaveBeenCalledTimes(callsBeforePause)

    await wrapper.findAll('button').find((button) => button.text().includes('继续'))!.trigger('click')
    await flushPromises()
    expect(api.readServiceLogs).toHaveBeenCalledTimes(callsBeforePause + 1)
    wrapper.unmount()
  })

  it('refreshing from another tab makes the event request immediately', async () => {
    const wrapper = mount(LogsPage)
    await flushPromises()
    const before = api.readEvents.mock.calls.length
    const refreshButton = wrapper.findAll('button').find((button) => button.text().includes('刷新'))!
    await refreshButton.trigger('click')
    await flushPromises()
    expect(api.readEvents.mock.calls.length).toBeGreaterThan(before)
    wrapper.unmount()
  })

  it('keeps previously loaded event pages during polling and does not pull a reader away from the current scroll position', async () => {
    let eventTotal = 250
    api.readEvents.mockImplementation(async (offset: number, limit: number) => offsetPage(offset, limit, eventTotal, eventRow))
    const wrapper = mount(LogsPage)
    await flushPromises()
    expect(wrapper.text()).toContain('source-150')

    await wrapper.get('.log-footer button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('source-50')
    expect(wrapper.findAll('.log-row')).toHaveLength(200)

    const region = wrapper.get('.log-list').element as HTMLElement
    const scrollTo = vi.fn()
    Object.defineProperties(region, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, writable: true, value: 10 },
      scrollTo: { configurable: true, value: scrollTo },
    })
    eventTotal = 260
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()

    expect(wrapper.text()).toContain('source-50')
    expect(wrapper.text()).toContain('source-259')
    expect(wrapper.findAll('.log-row')).toHaveLength(210)
    expect(scrollTo).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('single-flights concurrent older-page requests and prepends the page once', async () => {
    api.readEvents.mockImplementation(async (offset: number, limit: number) => offsetPage(offset, limit, 250, eventRow))
    const older = deferred<ReturnType<typeof offsetPage<ReturnType<typeof eventRow>>>>()
    api.readEvents.mockImplementation(async (offset: number, limit: number) => {
      if (offset === 50 && limit === 100) return older.promise
      return offsetPage(offset, limit, 250, eventRow)
    })
    const wrapper = mount(LogsPage)
    await flushPromises()
    const button = wrapper.get('.log-footer button')
    const firstClick = button.trigger('click')
    const secondClick = button.trigger('click')
    await Promise.all([firstClick, secondClick])
    await flushPromises()

    expect(api.readEvents.mock.calls.filter(([offset, limit]) => offset === 50 && limit === 100)).toHaveLength(1)
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
    older.resolve(offsetPage(50, 100, 250, eventRow))
    await flushPromises()
    expect((button.element as HTMLButtonElement).disabled).toBe(false)
    expect(wrapper.findAll('.log-row')).toHaveLength(200)
    expect(wrapper.text().match(/source-50\b/g)).toHaveLength(1)
    wrapper.unmount()
  })

  it('ignores an older-page response after the filter changes', async () => {
    api.readEvents.mockImplementation(async (offset: number, limit: number) => offsetPage(offset, limit, 250, eventRow))
    const older = deferred<ReturnType<typeof offsetPage<ReturnType<typeof eventRow>>>>()
    api.readEvents.mockImplementation(async (offset: number, limit: number) => {
      if (offset === 50 && limit === 100) return older.promise
      return offsetPage(offset, limit, 250, eventRow)
    })
    const wrapper = mount(LogsPage)
    await flushPromises()
    await wrapper.get('.log-footer button').trigger('click')
    await flushPromises()
    await wrapper.get('select[aria-label="过滤条件"]').setValue('failed')
    await flushPromises()
    older.resolve({ ...offsetPage(50, 100, 250, () => eventRow(-1, 'failed')), items: [eventRow(-1, 'failed')] })
    await flushPromises()

    expect(wrapper.text()).not.toContain('source--1')
    expect(wrapper.text()).toContain('没有失败记录')
    wrapper.unmount()
  })

  it('safely ignores late older-page responses and scheduled scroll work after unmount', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.push(callback); return frames.length })
    api.readEvents.mockImplementation(async (offset: number, limit: number) => offsetPage(offset, limit, 250, eventRow))
    const older = deferred<ReturnType<typeof offsetPage<ReturnType<typeof eventRow>>>>()
    api.readEvents.mockImplementation(async (offset: number, limit: number) => {
      if (offset === 50 && limit === 100) return older.promise
      return offsetPage(offset, limit, 250, eventRow)
    })
    const wrapper = mount(LogsPage)
    await flushPromises()
    await wrapper.get('.log-footer button').trigger('click')
    await flushPromises()

    wrapper.unmount()
    older.resolve(offsetPage(50, 100, 250, eventRow))
    for (const callback of frames.splice(0)) callback(0)
    await flushPromises()
    expect(api.readEvents.mock.calls.filter(([offset, limit]) => offset === 50 && limit === 100)).toHaveLength(1)
  })
})
