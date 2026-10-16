import { onScopeDispose, toValue, watch, type MaybeRefOrGetter } from 'vue'

/** Completion-based polling with cancellation, visibility handling, and single-flight refreshes. */
export function usePolling(
  callback: (signal: AbortSignal) => Promise<void>,
  intervalMs: number,
  enabled: MaybeRefOrGetter<boolean> = true,
  runImmediately = true,
): (restart?: boolean) => Promise<void> {
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let pending: Promise<void> | undefined
  const isEnabled = () => Boolean(toValue(enabled))
  const clearTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  const schedule = () => {
    clearTimer()
    if (!stopped && isEnabled() && (typeof document === 'undefined' || !document.hidden)) {
      timer = setTimeout(() => { void run().catch(() => {}) }, intervalMs)
    }
  }
  const run = (restart = false): Promise<void> => {
    if (stopped) return Promise.resolve()
    if (pending) {
      if (!restart) return pending
      controller?.abort()
      return pending.catch(() => {}).then(() => run())
    }
    clearTimer()
    const request = new AbortController()
    controller = request
    const current = Promise.resolve().then(() => {
      if (!stopped && !request.signal.aborted) return callback(request.signal)
    }).then(() => undefined).finally(() => {
      request.abort()
      if (pending === current) {
        pending = undefined
        controller = undefined
        schedule()
      }
    })
    pending = current
    return current
  }
  const visibilityChanged = () => {
    clearTimer()
    if (document.hidden) controller?.abort()
    else if (isEnabled()) void run(true).catch(() => {})
  }
  const stopWatching = watch(() => isEnabled(), (active, previous) => {
    clearTimer()
    if (!active) controller?.abort()
    else if (previous || runImmediately) void run(true).catch(() => {})
    else schedule()
  })
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visibilityChanged)
  if (isEnabled() && (typeof document === 'undefined' || !document.hidden)) {
    if (runImmediately) void run().catch(() => {})
    else schedule()
  }
  onScopeDispose(() => {
    stopped = true
    clearTimer()
    controller?.abort()
    stopWatching()
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibilityChanged)
  })
  return run
}
