'use client'

import { useCallback, useEffect, useRef } from 'react'

/** Poll after completion; callers own error display and ignore aborted results. */
export function usePolling(
  callback: (signal: AbortSignal) => Promise<void>,
  intervalMs: number,
  enabled = true,
) {
  const refreshRef = useRef<(restart?: boolean) => Promise<void>>(async () => {})
  const refresh = useCallback((restart = false) => refreshRef.current(restart), [])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let controller: AbortController | undefined
    let pending: Promise<void> | undefined
    const clearTimer = () => { clearTimeout(timer); timer = undefined }
    const schedule = () => {
      clearTimer()
      if (!stopped && enabled && !document.hidden) {
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
      pending = Promise.resolve().then(() => {
        if (!stopped && !request.signal.aborted) return callback(request.signal)
      }).finally(() => {
        // A failed Promise.all branch may have left a sibling request running.
        request.abort()
        pending = undefined
        controller = undefined
        schedule()
      })
      return pending
    }
    const visibilityChanged = () => {
      clearTimer()
      if (document.hidden) controller?.abort()
      else if (enabled) void run(true).catch(() => {})
    }
    refreshRef.current = run
    if (enabled && !document.hidden) void run().catch(() => {})
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      stopped = true
      clearTimer()
      controller?.abort()
      refreshRef.current = async () => {}
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [callback, intervalMs, enabled])

  return refresh
}
