import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'

export type ThemeId = 'blue' | 'emerald' | 'rose' | 'amber'
export type Appearance = 'light' | 'dark'
export interface ThemePreference { theme: ThemeId; appearance: Appearance }

export const THEME_OPTIONS: { id: ThemeId; name: string; hue: number }[] = [
  { id: 'blue', name: '电蓝', hue: 240 },
  { id: 'emerald', name: '翡翠', hue: 145 },
  { id: 'rose', name: '玫瑰', hue: 25 },
  { id: 'amber', name: '琥珀', hue: 85 },
]
const THEME_STORAGE_KEY = 'tgmusic.theme'
const isTheme = (value: unknown): value is ThemeId => THEME_OPTIONS.some((option) => option.id === value)
const isAppearance = (value: unknown): value is Appearance => value === 'light' || value === 'dark'

/** Accepts both the prior Zustand `{state,version}` envelope and plain preferences. */
export function parseThemePreference(raw: string | null, fallback: ThemePreference = { theme: 'blue', appearance: 'dark' }): ThemePreference {
  if (!raw) return { ...fallback }
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return { ...fallback }
    const value = (parsed as { state?: unknown }).state ?? parsed
    if (!value || typeof value !== 'object') return { ...fallback }
    const record = value as Record<string, unknown>
    return {
      theme: isTheme(record.theme) ? record.theme : fallback.theme,
      appearance: isAppearance(record.appearance) ? record.appearance : fallback.appearance,
    }
  } catch {
    return { ...fallback }
  }
}

function ramp(hue: number, appearance: Appearance): Record<number, string> {
  const rows = appearance === 'dark'
    ? [[50, 98, .015], [100, 95, .02], [200, 89, .025], [300, 79, .035], [400, 67, .04], [500, 55, .18], [600, 45, .15], [700, 38, .12], [800, 30, .08], [900, 22, .04]]
    : [[50, 97, .025], [100, 93, .05], [200, 86, .1], [300, 46, .15], [400, 55, .17], [500, 66, .18], [600, 57, .16], [700, 48, .13], [800, 39, .09], [900, 30, .05]]
  return Object.fromEntries(rows.map(([step, lightness, chroma]) => [step, `oklch(${lightness}% ${chroma} ${hue})`]))
}

export function applyThemePreference(preference: ThemePreference, target: HTMLElement | null = typeof document === 'undefined' ? null : document.documentElement): void {
  if (!target) return
  target.dataset.appearance = preference.appearance
  target.style.colorScheme = preference.appearance
  const theme = THEME_OPTIONS.find((option) => option.id === preference.theme) ?? THEME_OPTIONS[0]
  for (const [step, color] of Object.entries(ramp(theme.hue, preference.appearance))) {
    target.style.setProperty(`--color-accent-${step}`, color)
  }
}

export const useThemeStore = defineStore('theme', () => {
  let systemAppearance: Appearance = 'dark'
  if (typeof document !== 'undefined' && isAppearance(document.documentElement.dataset.appearance)) {
    systemAppearance = document.documentElement.dataset.appearance
  } else if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches) {
    systemAppearance = 'light'
  }
  let initial: ThemePreference = { theme: 'blue', appearance: systemAppearance }
  if (typeof window !== 'undefined') {
    try { initial = parseThemePreference(window.localStorage.getItem(THEME_STORAGE_KEY), initial) } catch { /* Storage can be unavailable. */ }
  }
  const theme = ref<ThemeId>(initial.theme)
  const appearance = ref<Appearance>(initial.appearance)
  const themeName = computed(() => THEME_OPTIONS.find((option) => option.id === theme.value)?.name ?? '电蓝')
  watch([theme, appearance], ([nextTheme, nextAppearance]) => {
    const preference = { theme: nextTheme, appearance: nextAppearance }
    applyThemePreference(preference)
    try { window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ state: preference, version: 0 })) } catch { /* Storage is optional. */ }
  }, { immediate: true, flush: 'sync' })
  function setTheme(value: ThemeId) { if (isTheme(value)) theme.value = value }
  function setAppearance(value: Appearance) { if (isAppearance(value)) appearance.value = value }
  return { theme, appearance, themeName, setTheme, setAppearance }
})
