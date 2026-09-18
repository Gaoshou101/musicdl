/**
 * Tailwind v4 runs as a PostCSS plugin. Without this file Next inlines
 * `@import "tailwindcss"` and ships the stylesheet as written: no utility class
 * is ever generated and every `@apply` reaches the browser unresolved, so the
 * panel renders as unstyled HTML while the build still reports success.
 */
const config = {
  plugins: ['@tailwindcss/postcss'],
}

export default config
