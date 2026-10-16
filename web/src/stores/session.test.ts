import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({
  bootstrapAdminSession: vi.fn(),
  login: vi.fn(),
  logoutAdmin: vi.fn(),
  errorMessage: vi.fn((error: unknown) => error instanceof Error ? error.message : 'error'),
}))
vi.mock('@/lib/api', () => api)
import { useSessionStore } from './session'

describe('admin session transitions', () => {
  beforeEach(() => { vi.clearAllMocks(); setActivePinia(createPinia()) })

  it('shares an in-flight bootstrap request across callers', async () => {
    let resolve!: (value: { authenticated: boolean; must_change: boolean; user_id: string }) => void
    api.bootstrapAdminSession.mockImplementation(() => new Promise((done) => { resolve = done }))
    const store = useSessionStore()
    const one = store.bootstrap()
    const two = store.bootstrap()
    expect(api.bootstrapAdminSession).toHaveBeenCalledTimes(1)
    resolve({ authenticated: true, must_change: true, user_id: 'admin' })
    await Promise.all([one, two])
    expect(store.authenticated).toBe(true)
    expect(store.mustChange).toBe(true)
  })

  it('keeps the authenticated state when logout fails, and clears it only after success', async () => {
    const store = useSessionStore()
    store.authenticated = true
    store.mustChange = true
    api.logoutAdmin.mockRejectedValueOnce(new Error('logout failed'))
    await expect(store.signOut()).rejects.toThrow('logout failed')
    expect(store.authenticated).toBe(true)
    expect(store.mustChange).toBe(true)
    api.logoutAdmin.mockResolvedValueOnce(undefined)
    await store.signOut()
    expect(store.authenticated).toBe(false)
    expect(store.mustChange).toBe(false)
  })
})
