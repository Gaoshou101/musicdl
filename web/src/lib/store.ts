import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type ThemeId = 'blue' | 'emerald' | 'rose' | 'amber'
export type Appearance = 'light' | 'dark'

/**
 * One accent ramp per theme and appearance.
 *
 * The whole ramp is written, not just the 500 step: every accent utility in
 * the panel is a sibling step (`bg-accent-500/20`, `text-accent-400`), so
 * moving one step moves one ring of the palette and leaves the rest coherent.
 */
function ramp(hue: number, appearance: Appearance = 'dark'): Record<string, string> {
  if (appearance === 'light') {
    return {
      50: `oklch(97% 0.025 ${hue})`,
      100: `oklch(93% 0.05 ${hue})`,
      200: `oklch(86% 0.1 ${hue})`,
      300: `oklch(46% 0.15 ${hue})`,
      400: `oklch(55% 0.17 ${hue})`,
      500: `oklch(66% 0.18 ${hue})`,
      600: `oklch(57% 0.16 ${hue})`,
      700: `oklch(48% 0.13 ${hue})`,
      800: `oklch(39% 0.09 ${hue})`,
      900: `oklch(30% 0.05 ${hue})`,
    }
  }

  return {
    50: `oklch(96% 0.02 ${hue})`,
    100: `oklch(92% 0.04 ${hue})`,
    200: `oklch(85% 0.08 ${hue})`,
    300: `oklch(75% 0.12 ${hue})`,
    400: `oklch(65% 0.15 ${hue})`,
    500: `oklch(55% 0.18 ${hue})`,
    600: `oklch(45% 0.15 ${hue})`,
    700: `oklch(38% 0.12 ${hue})`,
    800: `oklch(30% 0.08 ${hue})`,
    900: `oklch(22% 0.04 ${hue})`,
  }
}

export const THEMES: { id: ThemeId; name: string; hue: number }[] = [
  { id: 'blue', name: '电蓝', hue: 240 },
  { id: 'emerald', name: '翡翠', hue: 145 },
  { id: 'rose', name: '玫瑰', hue: 25 },
  { id: 'amber', name: '琥珀', hue: 85 },
]

const PALETTES: Record<ThemeId, Record<string, string>> = Object.fromEntries(
  THEMES.map((theme) => [theme.id, ramp(theme.hue)]),
) as Record<ThemeId, Record<string, string>>

const LIGHT_PALETTES: Record<ThemeId, Record<string, string>> = Object.fromEntries(
  THEMES.map((theme) => [theme.id, ramp(theme.hue, 'light')]),
) as Record<ThemeId, Record<string, string>>

function isThemeId(value: unknown): value is ThemeId {
  return typeof value === 'string' && value in PALETTES
}

export function isAppearance(value: unknown): value is Appearance {
  return value === 'light' || value === 'dark'
}

/** Resolve the first appearance without touching browser globals during SSR. */
export function resolveInitialAppearance(): Appearance {
  if (typeof document !== 'undefined') {
    const declared = document.documentElement.dataset.appearance
    if (isAppearance(declared)) return declared
  }

  if (typeof window !== 'undefined') {
    try {
      const raw = window.localStorage.getItem('tgmusic.theme')
      const stored = raw ? (JSON.parse(raw) as { state?: { appearance?: unknown } }) : null
      if (isAppearance(stored?.state?.appearance)) return stored.state.appearance
    } catch {
      // A blocked or malformed localStorage entry should never stop the panel.
    }

    if (window.matchMedia?.('(prefers-color-scheme: light)').matches) return 'light'
  }

  return 'dark'
}

/** Paint the appearance attribute before the next render. */
export function applyAppearance(appearance: Appearance | string | undefined): void {
  if (typeof document === 'undefined') return
  const resolved = isAppearance(appearance) ? appearance : resolveInitialAppearance()
  const root = document.documentElement
  root.dataset.appearance = resolved
  root.style.colorScheme = resolved
}

/** Paint one theme onto the document, so `accent-*` utilities follow it. */
export function applyTheme(theme: string | undefined, appearance?: Appearance): void {
  if (typeof document === 'undefined') return
  const resolvedTheme = isThemeId(theme) ? theme : 'blue'
  const resolvedAppearance = appearance ?? resolveInitialAppearance()
  const palette = resolvedAppearance === 'light' ? LIGHT_PALETTES[resolvedTheme] : PALETTES[resolvedTheme]
  const root = document.documentElement
  for (const [step, value] of Object.entries(palette)) {
    root.style.setProperty(`--color-accent-${step}`, value)
  }
}

/** The colour a theme is shown by in the sidebar's own swatch row. */
export function themeSwatch(theme: ThemeId): string {
  return PALETTES[theme]['500']
}

interface ThemeStore {
  theme: ThemeId
  appearance: Appearance
  setTheme: (theme: ThemeId) => void
  setAppearance: (appearance: Appearance) => void
}

export const useThemeStore = create<ThemeStore>()(
  persist(
    (set, get) => ({
      theme: 'blue',
      // Keep the server and the first client render deterministic. The root
      // layout's tiny pre-paint script selects the real appearance, then the
      // sidebar rehydrates this store after mount without a hydration mismatch.
      appearance: 'dark',
      setTheme: (theme) => {
        applyTheme(theme, get().appearance)
        set({ theme })
      },
      setAppearance: (appearance) => {
        const next = isAppearance(appearance) ? appearance : 'dark'
        applyAppearance(next)
        applyTheme(get().theme, next)
        set({ appearance: next })
      },
    }),
    {
      name: 'tgmusic.theme',
      // Rehydration is started by the mounted sidebar. That keeps persisted
      // state out of the server/client render comparison while the root script
      // prevents a canvas flash before React mounts.
      skipHydration: true,
      onRehydrateStorage: () => (state) => {
        // Zustand passes the merged default state to this callback even when
        // storage is empty. The root script has already resolved persisted or
        // system preference, so read that DOM decision instead of mistaking
        // the deterministic SSR default (`dark`) for a saved choice.
        const appearance = resolveInitialAppearance()
        applyAppearance(appearance)
        applyTheme(state?.theme, appearance)
      },
    },
  ),
)
