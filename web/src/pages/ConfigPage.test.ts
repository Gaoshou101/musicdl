import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ConfigPage from './ConfigPage.vue'

const api = vi.hoisted(() => ({
  AI_PROBE_TEXT: {
    disabled: 'AI 辅助还没打开',
    unconfigured: '缺少 API Key 或模型名',
    timeout: '端点在探针预算内没有回答',
    provider_error: '端点拒绝了这次请求',
    invalid_response: '端点回话了，但不是约定的 JSON',
  },
  errorMessage: (error: unknown) => error instanceof Error ? error.message : '未知错误',
  listConfig: vi.fn(),
  reloadNote: vi.fn((reload?: { status?: string }) => reload?.status === 'failed' ? '，但热重载失败：进程重启后生效' : ''),
  testAIEndpoint: vi.fn(),
  updateConfig: vi.fn(),
}))

vi.mock('@/lib/api', () => api)

const config = (loginLimit = 5) => ({
  groups: [
    {
      id: 'admin',
      label: '后台管理',
      help: '管理界面设置',
      fields: [
        {
          key: 'admin.login_limit', env: 'ADMIN_LOGIN_LIMIT', label: '窗口内失败次数上限',
          kind: 'int', scope: 'hot', help: '限制登录尝试', secret: false,
          source: 'panel', value: loginLimit, set: true,
        },
      ],
    },
    {
      id: 'ai',
      label: 'AI 排序',
      help: '可选的 AI 辅助功能',
      fields: [
        {
          key: 'ai.api_key', env: 'AI_API_KEY', label: 'API 密钥', kind: 'secret', scope: 'restart',
          help: '留空保持已保存的密钥', secret: true, source: 'panel', value: null, set: true,
        },
      ],
    },
  ],
  container: [{ key: 'data_dir', label: '数据目录', env: 'MUSICDL_DATA_DIR', help: '服务数据位置' }],
  rejected: { 'removed.option': '当前版本不支持' },
})

describe('ConfigPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listConfig.mockResolvedValue(config())
    api.updateConfig.mockImplementation(async (values: Record<string, unknown>) => ({
      ...config(typeof values['admin.login_limit'] === 'number' ? values['admin.login_limit'] : 5),
      reload: { status: 'skipped' },
    }))
    api.testAIEndpoint.mockResolvedValue({
      ok: true, code: null, detail: 'HTTP 200', took_ms: 85, model: 'small-model',
      budget_ms: 2500, reply: '{"ok":true}',
    })
  })

  it('loads dynamic fields and shows write-only and deployment-owned settings', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()

    expect(api.listConfig).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('窗口内失败次数上限')
    expect(wrapper.get('#config-ai-api_key').attributes('placeholder')).toBe('已设置；留空表示不修改')
    expect(wrapper.text()).toContain('容器与部署')
    expect(wrapper.text()).toContain('启动时丢弃的旧值')
    expect(wrapper.get('[id="config-admin-login_limit"]').element.tagName).toBe('INPUT')
    wrapper.unmount()
  })

  it('deduplicates simultaneous config refreshes', async () => {
    let finish: ((value: ReturnType<typeof config>) => void) | undefined
    api.listConfig.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const wrapper = mount(ConfigPage)
    await flushPromises()
    expect(api.listConfig).toHaveBeenCalledOnce()

    // The initial request remains pending and the refresh action stays locked.
    const refresh = wrapper.findAll('button').find((button) => button.text().includes('刷新'))!
    expect((refresh.element as HTMLButtonElement).disabled).toBe(true)
    expect(api.listConfig).toHaveBeenCalledOnce()
    finish?.(config())
    await flushPromises()
    wrapper.unmount()
  })

  it('locks fields and save while a refresh is pending, preserving the refreshed values', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()
    let finish: ((value: ReturnType<typeof config>) => void) | undefined
    api.listConfig.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))

    await wrapper.get('button.action-secondary').trigger('click')
    await flushPromises()
    const field = wrapper.get('#config-admin-login_limit')
    expect((field.element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.findAll('button').find((button) => button.text().includes('保存改动'))!.element as HTMLButtonElement).disabled).toBe(true)

    await field.setValue('6')
    finish?.(config(7))
    await flushPromises()

    expect((wrapper.get('#config-admin-login_limit').element as HTMLInputElement).value).toBe('7')
    expect(wrapper.text()).not.toContain('保存改动（1）')
    expect(api.updateConfig).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('locks fields and refresh while a save is pending, so in-flight edits cannot be lost', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()
    const field = wrapper.get('#config-admin-login_limit')
    await field.setValue('6')
    let finish: ((value: Awaited<ReturnType<typeof config>>) => void) | undefined
    api.updateConfig.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))

    await wrapper.findAll('button').find((button) => button.text().includes('保存改动'))!.trigger('click')
    await flushPromises()
    expect((field.element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.get('button.action-secondary').element as HTMLButtonElement).disabled).toBe(true)

    await field.setValue('7')
    finish?.(config(6))
    await flushPromises()

    expect(api.updateConfig).toHaveBeenCalledWith({ 'admin.login_limit': 6 })
    expect((wrapper.get('#config-admin-login_limit').element as HTMLInputElement).value).toBe('6')
    expect(wrapper.text()).toContain('已保存 1 项')
    expect(api.listConfig).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('submits typed dirty values without serializing a blank stored secret', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()
    await wrapper.get('[id="config-admin-login_limit"]').setValue('6')

    expect(wrapper.text()).toContain('保存改动（1）')
    await wrapper.findAll('button').find((button) => button.text().includes('保存改动'))!.trigger('click')
    await flushPromises()

    expect(api.updateConfig).toHaveBeenCalledWith({ 'admin.login_limit': 6 })
    expect(wrapper.text()).toContain('已保存 1 项')
    wrapper.unmount()
  })

  it('requires an explicit reset action to send null for an existing panel secret', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()
    const secretField = wrapper.get('#config-ai-api_key').element.closest('.grid')
    expect(secretField?.textContent).toContain('API 密钥')
    await wrapper.findAll('button').find((button) => button.text().includes('恢复为部署值') && button.element.closest('.grid') === secretField)!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('保存改动'))!.trigger('click')
    await flushPromises()

    expect(api.updateConfig).toHaveBeenCalledWith({ 'ai.api_key': null })
    wrapper.unmount()
  })

  it('rejects an invalid numeric draft as a whole before sending any request', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()
    await wrapper.get('[id="config-admin-login_limit"]').setValue('6.5')
    await wrapper.findAll('button').find((button) => button.text().includes('保存改动'))!.trigger('click')

    expect(wrapper.text()).toContain('必须是整数')
    expect(api.updateConfig).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('tests the saved AI endpoint and renders its status evidence', async () => {
    const wrapper = mount(ConfigPage)
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('测试连接'))!.trigger('click')
    await flushPromises()

    expect(api.testAIEndpoint).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('连接成功')
    expect(wrapper.text()).toContain('HTTP 200')
    expect(wrapper.text()).toContain('small-model')
    expect(wrapper.text()).toContain('{"ok":true}')
    wrapper.unmount()
  })
})
