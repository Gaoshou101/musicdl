import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { usePolling } from './usePolling'

const tick = async () => { await Promise.resolve(); await Promise.resolve() }

describe('usePolling', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('coalesces overlapping refreshes and aborts the active request on scope disposal', async () => {
    vi.useFakeTimers()
    const scope = effectScope()
    let refresh!: (restart?: boolean) => Promise<void>
    let signal!: AbortSignal
    let finish!: () => void
    const callback = vi.fn((request: AbortSignal) => {
      signal = request
      return new Promise<void>((resolve) => { finish = resolve })
    })
    scope.run(() => { refresh = usePolling(callback, 1000) })
    await tick()
    expect(callback).toHaveBeenCalledTimes(1)
    const first = refresh()
    const sameFlight = refresh()
    expect(first).toBe(sameFlight)
    scope.stop()
    expect(signal.aborted).toBe(true)
    finish()
    await first
  })

  it('restarts a running request serially after aborting its signal', async () => {
    const scope = effectScope()
    let refresh!: (restart?: boolean) => Promise<void>
    const signals: AbortSignal[] = []
    const finishes: (() => void)[] = []
    const callback = vi.fn((signal: AbortSignal) => {
      signals.push(signal)
      return new Promise<void>((resolve) => finishes.push(resolve))
    })
    scope.run(() => { refresh = usePolling(callback, 60_000) })
    await tick()
    const active = refresh()
    const restarted = refresh(true)
    expect(signals[0]!.aborted).toBe(true)
    expect(callback).toHaveBeenCalledTimes(1)
    finishes[0]!()
    await active
    await tick()
    expect(callback).toHaveBeenCalledTimes(2)
    expect(signals[1]!.aborted).toBe(false)
    finishes[1]!()
    await restarted
    scope.stop()
  })

  it('starts and stops polling when the enabled ref changes', async () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const enabled = ref(false)
    let refresh!: (restart?: boolean) => Promise<void>
    const callback = vi.fn(async () => {})
    scope.run(() => { refresh = usePolling(callback, 500, enabled, false) })
    enabled.value = true
    await tick()
    await vi.advanceTimersByTimeAsync(500)
    expect(callback).toHaveBeenCalledTimes(1)
    enabled.value = false
    await vi.advanceTimersByTimeAsync(1000)
    expect(callback).toHaveBeenCalledTimes(1)
    scope.stop()
    void refresh
  })

  it('aborts while the document is hidden and refreshes when it returns', async () => {
    const originalHidden = Object.getOwnPropertyDescriptor(document, 'hidden')
    let hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    const scope = effectScope()
    const signals: AbortSignal[] = []
    const callback = vi.fn((signal: AbortSignal) => new Promise<void>((resolve) => {
      signals.push(signal)
      signal.addEventListener('abort', () => resolve(), { once: true })
    }))
    scope.run(() => usePolling(callback, 60_000))
    await tick()
    hidden = true; document.dispatchEvent(new Event('visibilitychange'))
    expect(signals[0]!.aborted).toBe(true)
    await tick()
    hidden = false; document.dispatchEvent(new Event('visibilitychange'))
    await tick()
    expect(callback).toHaveBeenCalledTimes(2)
    scope.stop()
    if (originalHidden) Object.defineProperty(document, 'hidden', originalHidden)
    else Reflect.deleteProperty(document, 'hidden')
  })
})
