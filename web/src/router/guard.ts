export interface GuardSession { authenticated: boolean; mustChange: boolean }
export interface GuardRoute { requiresAuth: boolean; path: string; fullPath: string; name?: string | symbol | null }

/** Keep route policy separate from IO so forced-change and return-path behavior is testable. */
export function sessionRedirect(to: GuardRoute, session: GuardSession): string | { path: string; query?: Record<string, string> } | undefined {
  if (!to.requiresAuth) {
    if (to.path === '/' && session.authenticated) {
      return session.mustChange ? { path: '/dashboard/settings', query: { must_change: '1' } } : '/dashboard'
    }
    return undefined
  }
  if (!session.authenticated) return { path: '/', query: { next: to.fullPath } }
  if (session.mustChange && to.path !== '/dashboard/settings') return { path: '/dashboard/settings', query: { must_change: '1' } }
  return undefined
}
