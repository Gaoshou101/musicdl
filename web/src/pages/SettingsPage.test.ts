import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsPage from './SettingsPage.vue'

const dependencies = vi.hoisted(() => ({
  route: { query: {} as Record<string, unknown> },
  replace: vi.fn(),
  session: {
    mustChange: false,
    authenticated: true,
    credentialsChanged: vi.fn(),
  },
  changeCredentials: vi.fn(),
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
  changeCredentials: dependencies.changeCredentials,
  errorMessage: dependencies.errorMessage,
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function passwordInputs(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('form input').filter((input) => (input.element as HTMLInputElement).type === 'password')
}

async function fillCredentials(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('form input[autocomplete="username"]').setValue(' admin-next ')
  const [current, next, confirmation] = passwordInputs(wrapper)
  await current!.setValue('current-secret')
  await next!.setValue('new-password-123')
  await confirmation!.setValue('new-password-123')
}

describe('SettingsPage credential rotation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    dependencies.route.query = {}
    dependencies.session.mustChange = false
    dependencies.session.authenticated = true
    localStorage.clear()
  })

  it('sends one credential rotation for repeated submits and keeps the replacement session', async () => {
    const pending = deferred<void>()
    dependencies.changeCredentials.mockReturnValueOnce(pending.promise)
    const wrapper = mount(SettingsPage)
    await flushPromises()
    await fillCredentials(wrapper)
    const form = wrapper.get('form')

    await form.trigger('submit')
    expect(form.findAll('input').every((input) => (input.element as HTMLInputElement).disabled)).toBe(true)
    expect(passwordInputs(wrapper).every((input) => (input.element as HTMLInputElement).disabled)).toBe(true)
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(true)
    await form.trigger('submit')
    expect(dependencies.changeCredentials).toHaveBeenCalledOnce()
    expect(dependencies.changeCredentials).toHaveBeenCalledWith({
      username: 'admin-next', password: 'current-secret', newPassword: 'new-password-123',
    })

    pending.resolve()
    await flushPromises()
    expect(passwordInputs(wrapper).map((input) => (input.element as HTMLInputElement).value)).toEqual(['', '', ''])
    expect(dependencies.session.credentialsChanged).toHaveBeenCalledOnce()
    expect(dependencies.session.authenticated).toBe(true)
    expect(dependencies.replace).toHaveBeenCalledWith('/dashboard/settings')
    expect(wrapper.text()).toContain('当前浏览器已换用新的登录会话')
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })

  it('retains credentials and restores controls after an error so the user can retry', async () => {
    const pending = deferred<void>()
    dependencies.changeCredentials.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(undefined)
    const wrapper = mount(SettingsPage)
    await flushPromises()
    await fillCredentials(wrapper)
    const form = wrapper.get('form')
    const [, newPassword, confirmation] = passwordInputs(wrapper)

    await form.trigger('submit')
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(true)
    await form.trigger('submit')
    expect(dependencies.changeCredentials).toHaveBeenCalledOnce()
    pending.reject(new Error('凭据更新失败'))
    await flushPromises()

    expect(wrapper.text()).toContain('凭据更新失败')
    expect(passwordInputs(wrapper).map((input) => (input.element as HTMLInputElement).value)).toEqual([
      'current-secret', 'new-password-123', 'new-password-123',
    ])
    expect(passwordInputs(wrapper).every((input) => !(input.element as HTMLInputElement).disabled)).toBe(true)
    expect((form.get('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(false)

    await form.trigger('submit')
    await flushPromises()
    expect(dependencies.changeCredentials).toHaveBeenCalledTimes(2)
    expect(dependencies.session.credentialsChanged).toHaveBeenCalledOnce()
    expect(passwordInputs(wrapper).map((input) => (input.element as HTMLInputElement).value)).toEqual(['', '', ''])
    wrapper.unmount()
  })
})
