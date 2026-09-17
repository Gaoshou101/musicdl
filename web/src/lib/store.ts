import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type ThemeId = 'blue' | 'emerald' | 'rose' | 'amber'

/**
 * One accent ramp per theme.
 *
 * The whole ramp is written, not just the 500 step: every accent utility in
 * the panel is a sibling step (`bg-accent-500/20`, `text-accent-400`), so
 * moving one step moves one ring of the palette and leaves the rest blue.
 * The steps keep the shape the stylesheet declares and only rotate the hue.
 */
function ramp(hue: number): Record<string, string> {
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

function isThemeId(value: unknown): value is ThemeId {
  return typeof value === 'string' && value in PALETTES
}

/** Paint one theme onto the document, so `accent-*` utilities follow it. */
export function applyTheme(theme: string | undefined): void {
  if (typeof document === 'undefined') return
  const palette = PALETTES[isThemeId(theme) ? theme : 'blue']
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
  setTheme: (theme: ThemeId) => void
}

export const useThemeStore = create<ThemeStore>()(
  persist(
    (set) => ({
      theme: 'blue',
      setTheme: (theme) => {
        applyTheme(theme)
        set({ theme })
      },
    }),
    {
      name: 'tgmusic.theme',
      // A reload restores the stored choice, so the ramp has to be painted
      // again rather than waiting for the operator to click a swatch.
      onRehydrateStorage: () => (state) => applyTheme(state?.theme),
    },
  ),
)
