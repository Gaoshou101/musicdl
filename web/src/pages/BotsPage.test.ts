import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import BotsPage from './BotsPage.vue'

const api = vi.hoisted(() => ({
  beginTelegramLogin: vi.fn(),
  createBot: vi.fn(),
  deleteBot: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : '未知错误',
  listBots: vi.fn(),
  logoutTelegram: vi.fn(),
  readTelegram: vi.fn(),
  reloadNote: vi.fn(() => ''),
  submitTelegramPassword: vi.fn(),
  updateBot: vi.fn(),
  verifyTelegramLogin: vi.fn(),
}))

vi.mock('@/lib/api', () => api)

const bot = {
  id: 'vmusic', enabled: true, priority: 4, timeout: 8,
  username: 'vmusic_bot', command_template: '/search {query}',
}
const telegram = {
  enabled: true, profile: 'default', available: true, status: 'ready',
  retry_after: null, error: null, pending_phone: null,
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('BotsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('confirm', vi.fn(() => true))
    api.readTelegram.mockResolvedValue(telegram)
    api.listBots.mockResolvedValue({ items: [bot] })
    api.updateBot.mockImplementation(async (_id: string, changes: Partial<typeof bot>) => ({ ...bot, ...changes }))
    api.deleteBot.mockResolvedValue({ ...bot, reload: undefined })
  })

  it('toggles, edits, and removes a registered Bot through the API', async () => {
    const wrapper = mount(BotsPage)
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('停用'))!.trigger('click')
    await flushPromises()
    expect(api.updateBot).toHaveBeenCalledWith('vmusic', { enabled: false })

    await wrapper.findAll('button').find((button) => button.text().includes('编辑'))!.trigger('click')
    const edit = wrapper.get('article form')
    await edit.get('input').setValue('renamed_bot')
    await edit.findAll('input')[1].setValue('')
    await edit.findAll('input')[2].setValue('12')
    await edit.findAll('input')[3].setValue('9.5')
    await edit.trigger('submit')
    await flushPromises()
    expect(api.updateBot).toHaveBeenLastCalledWith('vmusic', {
      username: 'renamed_bot', command_template: null, priority: 12, timeout: 9.5,
    })

    await wrapper.findAll('button').find((button) => button.text().includes('删除'))!.trigger('click')
    await flushPromises()
    expect(window.confirm).toHaveBeenCalledWith('确定删除 Bot「vmusic」吗？')
    expect(api.deleteBot).toHaveBeenCalledWith('vmusic')
    expect(wrapper.text()).toContain('还没有注册 Bot')
    wrapper.unmount()
  })

  it('creates a Bot with numeric values and the built-in command default', async () => {
    api.listBots.mockResolvedValue({ items: [] })
    api.createBot.mockResolvedValue({ ...bot, id: 'newbot' })
    const wrapper = mount(BotsPage)
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('添加第一个 Bot'))!.trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    await dialog.findAll('input')[0].setValue('newbot')
    await dialog.findAll('input')[1].setValue('newbot_bot')
    await dialog.findAll('input')[2].setValue('  ')
    await dialog.findAll('input')[3].setValue('-2')
    await dialog.findAll('input')[4].setValue('6.5')
    await dialog.get('form').trigger('submit')
    await flushPromises()

    expect(api.createBot).toHaveBeenCalledWith({
      id: 'newbot', username: 'newbot_bot', command_template: undefined, priority: -2, timeout: 6.5,
    })
    expect(wrapper.text()).toContain('已添加 newbot')
    wrapper.unmount()
  })

  it('disables Bot creation until the first list request settles', async () => {
    const pending = deferred<{ items: Array<typeof bot> }>()
    api.listBots.mockReturnValueOnce(pending.promise)
    const wrapper = mount(BotsPage)
    const addButton = wrapper.findAll('button').find((button) => button.text().includes('添加 Bot'))!

    expect((addButton.element as HTMLButtonElement).disabled).toBe(true)
    await addButton.trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    pending.resolve({ items: [] })
    await flushPromises()
    expect((addButton.element as HTMLButtonElement).disabled).toBe(false)
    await addButton.trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('ignores a delayed Bot list snapshot after deleting a Bot', async () => {
    const wrapper = mount(BotsPage)
    await flushPromises()
    let finish: ((value: { items: Array<typeof bot> }) => void) | undefined
    api.listBots.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))

    await wrapper.get('[aria-label="刷新 Bot 列表"]').trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('删除'))!.trigger('click')
    await flushPromises()
    expect(api.deleteBot).toHaveBeenCalledWith('vmusic')

    finish?.({ items: [bot] })
    await flushPromises()
    expect(wrapper.text()).toContain('还没有注册 Bot')
    expect(wrapper.text()).not.toContain('@vmusic_bot')
    wrapper.unmount()
  })

  it('ignores a delayed empty Bot list snapshot after creating a Bot', async () => {
    api.listBots.mockResolvedValueOnce({ items: [] })
    api.createBot.mockResolvedValue({ ...bot, id: 'newbot', username: 'newbot_bot' })
    const wrapper = mount(BotsPage)
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('添加第一个 Bot'))!.trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    await dialog.findAll('input')[0].setValue('newbot')
    await dialog.findAll('input')[1].setValue('newbot_bot')
    let finish: ((value: { items: Array<typeof bot> }) => void) | undefined
    api.listBots.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))

    await wrapper.get('[aria-label="刷新 Bot 列表"]').trigger('click')
    await flushPromises()
    await dialog.get('form').trigger('submit')
    await flushPromises()
    expect(api.createBot).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('@newbot_bot')

    finish?.({ items: [] })
    await flushPromises()
    expect(wrapper.text()).toContain('@newbot_bot')
    wrapper.unmount()
  })
})
