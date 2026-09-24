import type { Metadata } from 'next'
import '../styles/globals.css'

const appearanceInitScript = `
(function () {
  var appearance = 'dark';
  try {
    var raw = window.localStorage.getItem('tgmusic.theme');
    var stored = raw ? JSON.parse(raw) : null;
    var saved = stored && stored.state && stored.state.appearance;
    if (saved === 'light' || saved === 'dark') {
      appearance = saved;
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
      appearance = 'light';
    }
  } catch (error) {
    // An unavailable or malformed preference should never block first paint.
  }
  document.documentElement.dataset.appearance = appearance;
})();
`

export const metadata: Metadata = {
  title: 'tgmusic 管理后台',
  description: 'Telegram 音乐机器人管理面板',
  icons: {
    icon: '/brand/icon.svg',
    shortcut: '/brand/icon.svg',
    apple: '/brand/icon-apple-touch.png',
  },
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script id="appearance-init" dangerouslySetInnerHTML={{ __html: appearanceInitScript }} />
      </head>
      <body className="font-sans">{children}</body>
    </html>
  )
}
