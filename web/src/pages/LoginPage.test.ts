import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import LoginPage from './LoginPage.vue'

const dependencies = vi.hoisted(() => ({
  route: { query: {} as Record<string, unknown> },
  replace: vi.fn(),
  session: {
    error: '',
    mustChange: false,
    authenticated: false,
    signIn: vi.fn(),
  },
  USERNAME_STORAGE_KEY: 'musicdl.username',
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : '未知错误',
}))

vi.mock('vue-router', () => ({
  useRoute: () => dependencies.route,
  useRouter: () => ({ replace: dependencies.replace }),
}))
vi.mock('@/stores/session', () => ({ useSessionStore: () => dependencies.session }))
vi.mock('@/lib/api', () => ({
  USERNAME_STORAGE_KEY: dependencies.USERNAME_STORAGE_KEY,
  errorMessage: dependencies.errorMessage,
}))
vi.mock('@/components/ThemeControl.vue', () => ({ default: { template: '<div />' } }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    dependencies.route.query = {}
    dependencies.session.error = ''
    dependencies.session.mustChange = false
    dependencies.session.authenticated = false
    localStorage.clear()
  })

  it('blocks duplicate submits during login and clears the password after success', async () => {
    const pending = deferred<{ must_change: boolean }>()
    dependencies.session.signIn.mockReturnValueOnce(pending.promise)
    const wrapper = mount(LoginPage)
    await flushPromises()
    await wrapper.get('[name="username"]').setValue(' admin ')
    await wrapper.get('[name="password"]').setValue('current-secret')

    const form = wrapper.get('form')
    await form.trigger('submit')
    expect((wrapper.get('[name="username"]').element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.get('[name="password"]').element as HTMLInputElement).disabled).toBe(true)
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(true)
    await form.trigger('submit')
    expect(dependencies.session.signIn).toHaveBeenCalledOnce()

    pending.resolve({ must_change: false })
    await flushPromises()
    expect(dependencies.session.signIn).toHaveBeenCalledWith('admin', 'current-secret', false)
    expect((wrapper.get('[name="password"]').element as HTMLInputElement).value).toBe('')
    expect(dependencies.replace).toHaveBeenCalledWith('/dashboard')
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })

  it('keeps credentials and restores login controls after a failed request', async () => {
    dependencies.session.signIn.mockRejectedValueOnce(new Error('登录失败')).mockResolvedValueOnce({ must_change: true })
    const wrapper = mount(LoginPage)
    await flushPromises()
    await wrapper.get('[name="password"]').setValue('current-secret')
    const form = wrapper.get('form')
    await form.trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('登录失败')
    expect((wrapper.get('[name="password"]').element as HTMLInputElement).value).toBe('current-secret')
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(false)

    await form.trigger('submit')
    await flushPromises()
    expect(dependencies.session.signIn).toHaveBeenCalledTimes(2)
    expect(dependencies.replace).toHaveBeenLastCalledWith({ path: '/dashboard/settings', query: { must_change: '1' } })
    wrapper.unmount()
  })
})
