import { describe, expect, it } from 'vitest'
import { botTemplateForCreate, botTemplateForUpdate, parseBotDraft, type BotDraft } from './botDraft'

const draft = (changes: Partial<BotDraft> = {}): BotDraft => ({
  id: 'vmusic',
  username: 'vmusic_bot',
  command_template: '',
  priority: '0',
  timeout: '10',
  ...changes,
})

describe('bot draft mapping', () => {
  it('accepts the configured priority and timeout limits', () => {
    expect(parseBotDraft(draft({ priority: '-1000', timeout: '0.1' }))).toEqual({
      ok: true,
      value: { priority: -1000, timeout: 0.1 },
    })
    expect(parseBotDraft(draft({ priority: '1000', timeout: '120' })).ok).toBe(true)
  })

  it.each([
    [{ priority: '1001' }, '优先级必须是 -1000 到 1000 之间的整数'],
    [{ priority: '1.5' }, '优先级必须是 -1000 到 1000 之间的整数'],
    [{ timeout: '0' }, '超时必须是 0.1 到 120 秒之间的数字'],
    [{ timeout: '120.1' }, '超时必须是 0.1 到 120 秒之间的数字'],
    [{ timeout: 'nope' }, '超时必须是 0.1 到 120 秒之间的数字'],
  ] as const)('blocks invalid numeric draft %o', (changes, text) => {
    expect(parseBotDraft(draft(changes))).toEqual({ ok: false, text })
  })

  it('keeps create and edit empty-template semantics distinct', () => {
    expect(botTemplateForCreate('  ')).toBeUndefined()
    expect(botTemplateForUpdate('  ')).toBeNull()
    expect(botTemplateForUpdate(' /find {query} ')).toBe('/find {query}')
  })
})
