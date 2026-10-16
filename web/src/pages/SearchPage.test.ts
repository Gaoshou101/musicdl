import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SearchPage from './SearchPage.vue'

const routeState = vi.hoisted(() => ({ query: {} as Record<string, unknown> }))
const api = vi.hoisted(() => ({
  downloadCandidate: vi.fn(),
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : '未知错误',
  mediaUrl: (path: string) => '/media/' + path,
  probeCandidateSizes: vi.fn(),
  searchCandidates: vi.fn(),
}))

vi.mock('vue-router', () => ({
  RouterLink: { template: '<a><slot /></a>' },
  useRoute: () => ({ query: routeState.query }),
}))
vi.mock('@/lib/api', () => api)

const candidate = (id: string, title = id) => ({
  source_id: 'demo', source_version: '1', item_id: id, title, artist: 'artist', format: 'flac',
})
const report = (query: string, candidates: ReturnType<typeof candidate>[]) => ({
  query, version: '1', count: candidates.length, total: candidates.length, candidates, sources: [],
})
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((finish) => { resolve = finish })
  return { promise, resolve }
}

describe('SearchPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    routeState.query = {}
  })

  it('clears previously rendered results while a new query is pending', async () => {
    const second = deferred<ReturnType<typeof report>>()
    api.searchCandidates.mockResolvedValueOnce(report('first', [candidate('first-song', 'First song')]))
      .mockReturnValueOnce(second.promise)
    const wrapper = mount(SearchPage)

    await wrapper.get('#search-query').setValue('first')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('First song')

    const input = wrapper.get('#search-query')
    ;(input.element as HTMLInputElement).value = 'second'
    await input.trigger('input')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(api.searchCandidates).toHaveBeenLastCalledWith('second', 20, expect.any(AbortSignal))
    expect(wrapper.text()).not.toContain('First song')
    second.resolve(report('second', [candidate('second-song', 'Second song')]))
    await flushPromises()
    expect(wrapper.text()).toContain('Second song')
    wrapper.unmount()
  })

  it('aborts an older query and ignores its late response', async () => {
    const older = deferred<ReturnType<typeof report>>()
    api.searchCandidates.mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce(report('newer', [candidate('newer-song', 'Newer song')]))
    const wrapper = mount(SearchPage)

    await wrapper.get('#search-query').setValue('older')
    await wrapper.get('form').trigger('submit')
    const olderSignal = api.searchCandidates.mock.calls[0]![2] as AbortSignal
    const input = wrapper.get('#search-query')
    ;(input.element as HTMLInputElement).value = 'newer'
    await input.trigger('input')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(olderSignal.aborted).toBe(true)
    older.resolve(report('older', [candidate('older-song', 'Stale older song')]))
    await flushPromises()
    expect(wrapper.text()).toContain('Newer song')
    expect(wrapper.text()).not.toContain('Stale older song')
    wrapper.unmount()
  })

  it('probes the first ten results and cancels an old probe when the next search is empty', async () => {
    const songs = Array.from({ length: 11 }, (_, index) => candidate('song-' + index, 'Song ' + index))
    api.searchCandidates.mockResolvedValueOnce(report('song', songs)).mockResolvedValueOnce(report('empty', []))
    let probeSignal!: AbortSignal
    api.probeCandidateSizes.mockImplementation((_candidates: unknown, signal: AbortSignal) => {
      probeSignal = signal
      return new Promise(() => {})
    })
    const wrapper = mount(SearchPage)

    await wrapper.get('#search-query').setValue('song')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('探测大小'))!.trigger('click')
    await flushPromises()

    expect(api.probeCandidateSizes).toHaveBeenCalledWith(songs.slice(0, 10), expect.any(AbortSignal))
    const input = wrapper.get('#search-query')
    ;(input.element as HTMLInputElement).value = 'empty'
    await input.trigger('input')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(probeSignal.aborted).toBe(true)
    expect(wrapper.text()).toContain('没有找到结果')
    expect(wrapper.text()).not.toContain('Song 0')
    expect(api.probeCandidateSizes).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
})
