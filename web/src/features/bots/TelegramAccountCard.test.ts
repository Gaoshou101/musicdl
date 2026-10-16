import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TelegramAccountCard from './TelegramAccountCard.vue'

const api = vi.hoisted(() => ({
  beginTelegramLogin: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : '未知错误',
  logoutTelegram: vi.fn(),
  readTelegram: vi.fn(),
  submitTelegramPassword: vi.fn(),
  verifyTelegramLogin: vi.fn(),
}))

vi.mock('@/lib/api', () => api)

const telegram = (overrides: Record<string, unknown> = {}) => ({
  enabled: true,
  profile: 'default',
  available: true,
  status: 'invalid_session',
  retry_after: null,
  error: null,
  pending_phone: null,
  ...overrides,
})

describe('Telegram account sign-in card', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
    api.readTelegram.mockResolvedValue(telegram())
  })

  it('completes the phone, code, two-step password, and logout flow', async () => {
    api.beginTelegramLogin.mockResolvedValue(telegram({ status: 'code_required', pending_phone: '+86****0000' }))
    api.verifyTelegramLogin.mockResolvedValue(telegram({ status: 'password_required', pending_phone: '+86****0000' }))
    api.submitTelegramPassword.mockResolvedValue(telegram({ status: 'ready' }))
    api.logoutTelegram.mockResolvedValue(telegram({ status: 'invalid_session' }))
    const wrapper = mount(TelegramAccountCard)
    await flushPromises()

    await wrapper.get('#telegram-phone').setValue('+8613800138000')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.beginTelegramLogin).toHaveBeenCalledWith('+8613800138000')
    expect(wrapper.text()).toContain('等待输入登录码')

    await wrapper.get('#telegram-code').setValue('12345')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.verifyTelegramLogin).toHaveBeenCalledWith('12345')
    expect(wrapper.get('#telegram-password').element).toBeDefined()

    await wrapper.get('#telegram-password').setValue('two-factor-secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.submitTelegramPassword).toHaveBeenCalledWith('two-factor-secret')
    expect(wrapper.text()).toContain('账号已授权')

    await wrapper.findAll('button').find((button) => button.text().includes('退出登录'))!.trigger('click')
    await flushPromises()
    expect(api.logoutTelegram).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('未登录')
    wrapper.unmount()
  })

  it('does not start sign-in while the initial Telegram status read is pending', async () => {
    let finish: ((value: ReturnType<typeof telegram>) => void) | undefined
    api.readTelegram.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    api.beginTelegramLogin.mockResolvedValue(telegram({ status: 'code_required', pending_phone: '+86****0000' }))
    const wrapper = mount(TelegramAccountCard)
    await flushPromises()

    const phone = wrapper.get('#telegram-phone')
    expect((phone.element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.get('form button[type="submit"]').element as HTMLButtonElement).disabled).toBe(true)
    await phone.setValue('+8613800138000')
    await wrapper.get('form').trigger('submit')
    expect(api.beginTelegramLogin).not.toHaveBeenCalled()

    finish?.(telegram())
    await flushPromises()
    expect((wrapper.get('#telegram-phone').element as HTMLInputElement).disabled).toBe(false)

    await wrapper.get('#telegram-phone').setValue('+8613800138000')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.beginTelegramLogin).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('等待输入登录码')
    wrapper.unmount()
  })

  it('clears a failed two-step password and requires re-entry instead of offering retry', async () => {
    api.readTelegram.mockResolvedValue(telegram({ status: 'password_required', pending_phone: '+86****0000' }))
    api.submitTelegramPassword.mockRejectedValueOnce(new Error('密码验证失败')).mockResolvedValueOnce(
      telegram({ status: 'ready' }),
    )
    const wrapper = mount(TelegramAccountCard)
    await flushPromises()

    await wrapper.get('#telegram-password').setValue('first-secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.submitTelegramPassword).toHaveBeenNthCalledWith(1, 'first-secret')
    expect((wrapper.get('#telegram-password').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('密码验证失败')
    expect(wrapper.findAll('button').some((button) => button.text().includes('重试上次操作'))).toBe(false)

    await wrapper.get('#telegram-password').setValue('second-secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.submitTelegramPassword).toHaveBeenNthCalledWith(2, 'second-secret')
    expect(wrapper.text()).toContain('账号已授权')
    wrapper.unmount()
  })

  it('clears a flood-limited two-step password and permits a fresh entry after the wait', async () => {
    vi.useFakeTimers()
    api.readTelegram.mockResolvedValue(telegram({ status: 'password_required', pending_phone: '+86****0000' }))
    api.submitTelegramPassword.mockResolvedValueOnce(telegram({
      status: 'rate_limited', retry_after: 2, error: 'Flood wait', pending_phone: '+86****0000',
    })).mockResolvedValueOnce(telegram({ status: 'ready' }))
    const wrapper = mount(TelegramAccountCard)
    await flushPromises()

    await wrapper.get('#telegram-password').setValue('first-secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect((wrapper.get('#telegram-password').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('重新输入两步验证密码')
    expect(wrapper.text()).not.toContain('重试上次操作')
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()

    await wrapper.get('#telegram-password').setValue('second-secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.submitTelegramPassword).toHaveBeenNthCalledWith(2, 'second-secret')
    expect(wrapper.text()).toContain('账号已授权')
    wrapper.unmount()
  })

  it('shows Telegram flood-wait feedback and blocks a new code request until retry time passes', async () => {
    vi.useFakeTimers()
    api.readTelegram.mockResolvedValue(telegram({
      status: 'rate_limited',
      retry_after: 2,
      error: 'Flood wait',
    }))
    const wrapper = mount(TelegramAccountCard)
    await flushPromises()

    expect(wrapper.text()).toContain('Flood wait')
    expect(wrapper.text()).toContain('请 2 秒后重试')
    await wrapper.get('#telegram-phone').setValue('+8613800138000')
    expect((wrapper.get('form button[type="submit"]').element as HTMLButtonElement).disabled).toBe(true)

    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.text()).toContain('发送登录码')
    expect((wrapper.get('form button[type="submit"]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })

  it('keeps the last action available for an explicit retry after a request failure', async () => {
    api.beginTelegramLogin.mockRejectedValueOnce(new Error('验证码请求失败')).mockResolvedValueOnce(
      telegram({ status: 'code_required', pending_phone: '+86****0000' }),
    )
    const wrapper = mount(TelegramAccountCard)
    await flushPromises()
    await wrapper.get('#telegram-phone').setValue('+8613800138000')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('验证码请求失败')
    await wrapper.findAll('button').find((button) => button.text().includes('重试上次操作'))!.trigger('click')
    await flushPromises()
    expect(api.beginTelegramLogin).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('登录码已发送')
    wrapper.unmount()
  })
})
