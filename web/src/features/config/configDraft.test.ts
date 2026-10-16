import { describe, expect, it } from 'vitest'
import type { ConfigFieldView, ConfigReport } from '@/lib/api'
import {
  collectConfigChanges,
  configGroupIsDirty,
  createConfigDrafts,
  isConfigFieldDirty,
  type ConfigDrafts,
} from './configDraft'

function field(overrides: Partial<ConfigFieldView> & Pick<ConfigFieldView, 'key' | 'kind'>): ConfigFieldView {
  return {
    key: overrides.key,
    env: overrides.env ?? overrides.key.toUpperCase(),
    label: overrides.label ?? overrides.key,
    kind: overrides.kind,
    scope: overrides.scope ?? 'hot',
    help: overrides.help ?? '',
    secret: overrides.secret ?? false,
    source: overrides.source ?? 'default',
    value: overrides.value ?? null,
    set: overrides.set ?? false,
  }
}

function report(fields: ConfigFieldView[]): ConfigReport {
  return {
    groups: [{ id: 'ai', label: 'AI', help: '', fields }],
    container: [],
    rejected: {},
  }
}

describe('dynamic configuration drafts', () => {
  it('leaves an existing write-only secret untouched when the input stays blank', () => {
    const secret = field({ key: 'ai.api_key', kind: 'secret', secret: true, set: true, source: 'panel' })
    const config = report([secret])
    const drafts = createConfigDrafts(config)

    expect(drafts[secret.key]).toBe('')
    expect(isConfigFieldDirty(secret, drafts, [])).toBe(false)
    expect(collectConfigChanges(config, drafts, []).values).toEqual({})
  })

  it('writes a newly entered secret and uses null only for an explicit deployment reset', () => {
    const secret = field({ key: 'ai.api_key', kind: 'secret', secret: true, set: true, source: 'panel' })
    const config = report([secret])
    const drafts: ConfigDrafts = { [secret.key]: 'replacement-key' }

    expect(collectConfigChanges(config, drafts, []).values).toEqual({ [secret.key]: 'replacement-key' })
    expect(collectConfigChanges(config, createConfigDrafts(config), [secret.key]).values).toEqual({
      [secret.key]: null,
    })
  })

  it('converts dynamic number, list and boolean fields into the API payload types', () => {
    const fields = [
      field({ key: 'admin.login_limit', kind: 'int', value: 5 }),
      field({ key: 'audio.timeout', kind: 'float', value: 3.5 }),
      field({ key: 'network.proxies', kind: 'list', value: ['http://a', 'http://b'] }),
      field({ key: 'telegram.enabled', kind: 'bool', value: false }),
    ]
    const config = report(fields)
    const drafts: ConfigDrafts = {
      'admin.login_limit': '6',
      'audio.timeout': '4.25',
      'network.proxies': 'http://a, , http://c ',
      'telegram.enabled': true,
    }

    expect(collectConfigChanges(config, drafts, []).values).toEqual({
      'admin.login_limit': 6,
      'audio.timeout': 4.25,
      'network.proxies': ['http://a', 'http://c'],
      'telegram.enabled': true,
    })
  })

  it('rejects invalid numbers before returning any batch changes', () => {
    const fields = [
      field({ key: 'admin.login_limit', kind: 'int', value: 5 }),
      field({ key: 'download.timeout', kind: 'float', value: 3.5 }),
    ]
    const config = report(fields)
    const result = collectConfigChanges(config, { 'admin.login_limit': '6.5', 'download.timeout': '4' }, [])

    expect(result).toEqual({ values: {}, problem: '「admin.login_limit」必须是整数' })
  })

  it('tracks dirty fields and clears per-group independently', () => {
    const fields = [
      field({ key: 'ai.enabled', kind: 'bool', value: false }),
      field({ key: 'ai.model', kind: 'text', value: 'old' }),
    ]
    const config = report(fields)
    const drafts = createConfigDrafts(config)
    drafts['ai.model'] = 'new'

    expect(configGroupIsDirty(config, 'ai', drafts, [])).toBe(true)
    expect(configGroupIsDirty(config, 'missing', drafts, [])).toBe(false)
  })
})
