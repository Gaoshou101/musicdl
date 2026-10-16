import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { applyThemePreference, parseThemePreference, useThemeStore } from './theme'

describe('theme persistence and migration', () => {
  beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()) })

  it('reads the legacy Zustand envelope without losing the selected palette', () => {
    expect(parseThemePreference(JSON.stringify({ state: { theme: 'rose', appearance: 'light' }, version: 0 })))
      .toEqual({ theme: 'rose', appearance: 'light' })
  })

  it('falls back safely when stored content is malformed or has an unknown theme', () => {
    expect(parseThemePreference('{not-json')).toEqual({ theme: 'blue', appearance: 'dark' })
    expect(parseThemePreference(JSON.stringify({ theme: 'neon', appearance: 'sepia' })))
      .toEqual({ theme: 'blue', appearance: 'dark' })
  })

  it('applies appearance and accent colors immediately, then persists changes in the legacy envelope', () => {
    localStorage.setItem('tgmusic.theme', JSON.stringify({ state: { theme: 'emerald', appearance: 'light' }, version: 0 }))
    const store = useThemeStore()
    expect(store.theme).toBe('emerald')
    expect(document.documentElement.dataset.appearance).toBe('light')
    expect(document.documentElement.style.getPropertyValue('--color-accent-500')).toContain('145')
    store.setAppearance('dark')
    expect(parseThemePreference(localStorage.getItem('tgmusic.theme'))).toEqual({ theme: 'emerald', appearance: 'dark' })
  })

  it('uses the appearance selected by the pre-paint script when no saved preference exists', () => {
    document.documentElement.dataset.appearance = 'light'
    const store = useThemeStore()
    expect(store.appearance).toBe('light')
  })

  it('can paint a specific target without depending on global document state', () => {
    const target = document.createElement('html')
    applyThemePreference({ theme: 'amber', appearance: 'dark' }, target)
    expect(target.dataset.appearance).toBe('dark')
    expect(target.style.getPropertyValue('--color-accent-500')).toContain('85')
  })
})
