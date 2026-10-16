import { describe, expect, it } from 'vitest'
import { sessionRedirect } from './guard'

describe('session route policy', () => {
  it('returns an unauthenticated deep link to the login form', () => {
    expect(sessionRedirect({ requiresAuth: true, path: '/dashboard/logs', fullPath: '/dashboard/logs?tab=service' }, { authenticated: false, mustChange: false }))
      .toEqual({ path: '/', query: { next: '/dashboard/logs?tab=service' } })
  })

  it('allows a forced credential change only on settings', () => {
    const session = { authenticated: true, mustChange: true }
    expect(sessionRedirect({ requiresAuth: true, path: '/dashboard/health', fullPath: '/dashboard/health' }, session))
      .toEqual({ path: '/dashboard/settings', query: { must_change: '1' } })
    expect(sessionRedirect({ requiresAuth: true, path: '/dashboard/settings', fullPath: '/dashboard/settings?must_change=1' }, session)).toBeUndefined()
  })

  it('sends an authenticated user away from login to the required destination', () => {
    expect(sessionRedirect({ requiresAuth: false, path: '/', fullPath: '/' }, { authenticated: true, mustChange: true }))
      .toEqual({ path: '/dashboard/settings', query: { must_change: '1' } })
    expect(sessionRedirect({ requiresAuth: false, path: '/', fullPath: '/' }, { authenticated: true, mustChange: false })).toBe('/dashboard')
  })
})
