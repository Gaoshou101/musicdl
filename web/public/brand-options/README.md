# musicdl brand options

These are three self-contained, minimal visual directions for the musicdl
project. Each direction uses one centered rounded music note with generous
negative space and no copy inside the artwork. The original filenames remain
so existing links keep working.

## Directions

| Direction | Palette | Icon | Banner | Social preview |
| --- | --- | --- | --- | --- |
| Cool porcelain / frost (legacy cool filename) | Frost white, pale ice blue, and deep navy | `icon-cool-aurora-glass.svg` | `banner-cool-aurora-glass.svg` | `social-preview-cool-aurora-glass.png` |
| Warm ivory / champagne / cocoa (legacy warm filename) | Warm ivory, champagne, and cocoa brown | `icon-warm-sunrise-glass.svg` | `banner-warm-sunrise-glass.svg` | `social-preview-warm-sunrise-glass.png` |
| Midnight blue / quiet silver (legacy monochrome filename) | Muted blue-black and silvery white | `icon-monochrome-prism.svg` | `banner-monochrome-prism.svg` | `social-preview-monochrome-prism.png` |

All three directions share one centered rounded eighth-note glyph, with palette
and material providing the visual variation. The mark stays unmistakable at
small sizes, with a quiet tonal background and high contrast. The icon is a
1024x1024 rounded square. Each banner is 1600x900. Each social preview is a
1280x640 PNG with a centered crop that keeps the note in frame.

## Vector and raster workflow

The SVGs are hand-authored vector sources. Keep the note centered when tuning
the palette or silhouette, and preserve the dimensions above. The current
Sharp pipelines for the 1280x640 social previews are:

| Direction | Current pipeline |
| --- | --- |
| Cool porcelain / frost | Render the 1600x900 banner, take a centered 1600x800 crop with `top=50`, then resize to 1280x640. |
| Warm ivory / champagne / cocoa | Take a centered 1280x640 crop directly from the 1600x900 banner. |
| Midnight blue / quiet silver | Take a centered 1280x640 crop directly from the 1600x900 banner. |

The resulting PNG keeps the same centered glyph as its SVG source while
preserving each direction's intended framing.

## Live paths

These paths resolve from the Next public directory at runtime:

- `/brand-options/icon-cool-aurora-glass.svg`
- `/brand-options/banner-cool-aurora-glass.svg`
- `/brand-options/social-preview-cool-aurora-glass.png`
- `/brand-options/icon-warm-sunrise-glass.svg`
- `/brand-options/banner-warm-sunrise-glass.svg`
- `/brand-options/social-preview-warm-sunrise-glass.png`
- `/brand-options/icon-monochrome-prism.svg`
- `/brand-options/banner-monochrome-prism.svg`
- `/brand-options/social-preview-monochrome-prism.png`
