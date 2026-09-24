'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useEffect } from 'react'
import {
  House,
  MusicNotes,
  Robot,
  Pulse,
  Article,
  Gear,
  Moon,
  Palette,
  Sliders,
  Sun,
} from '@phosphor-icons/react'
import { THEMES, themeSwatch, useThemeStore } from '@/lib/store'

const navItems = [
  { href: '/dashboard', icon: House, label: '仪表板' },
  { href: '/dashboard/sources', icon: MusicNotes, label: '音源管理' },
  { href: '/dashboard/bots', icon: Robot, label: 'Bot 管理' },
  { href: '/dashboard/config', icon: Sliders, label: '运行配置' },
  { href: '/dashboard/health', icon: Pulse, label: '健康监控' },
  { href: '/dashboard/logs', icon: Article, label: '日志查看' },
  { href: '/dashboard/settings', icon: Gear, label: '设置' },
]

export function Sidebar() {
  const pathname = usePathname()
  const theme = useThemeStore((state) => state.theme)
  const setTheme = useThemeStore((state) => state.setTheme)

  return (
    <aside aria-label="主导航" className="w-64 max-sm:w-[4.75rem] h-screen shrink-0 glass border-r border-neutral-800 flex flex-col">
      {/* Logo */}
      <div className="p-6 max-sm:p-3 border-b border-neutral-800">
        <div className="flex items-center gap-3">
          <img
            src="/brand/icon.svg"
            alt=""
            aria-hidden="true"
            width={40}
            height={40}
            className="h-10 w-10 rounded-lg object-cover shadow-sm"
          />
          <div className="max-sm:hidden">
            <h2 className="font-semibold">tgmusic</h2>
            <p className="text-xs text-neutral-400">管理后台</p>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav aria-label="后台导航" className="flex-1 p-4 max-sm:p-2 space-y-1 overflow-y-auto">
        {navItems.map((item) => {
          const isActive = pathname === item.href
          const Icon = item.icon
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-4 py-3 max-sm:justify-center max-sm:px-2 rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-neutral-950 ${
                isActive
                  ? 'bg-accent-500/15 text-accent-300'
                  : 'text-neutral-300 hover:bg-neutral-800/50 hover:text-neutral-100'
              }`}
            >
              <Icon size={20} weight={isActive ? 'duotone' : 'regular'} />
              <span className="font-medium text-sm max-sm:hidden">{item.label}</span>
            </Link>
          )
        })}
      </nav>

      {/* Theme selector */}
      <div className="p-4 max-sm:p-3 border-t border-neutral-800">
        <div className="flex items-center gap-2 mb-3">
          <Palette size={16} className="text-neutral-400" />
          <span className="text-xs font-medium text-neutral-400 max-sm:hidden">配色主题</span>
        </div>
        <div className="grid grid-cols-4 max-sm:grid-cols-2 gap-2 max-sm:gap-1">
          {THEMES.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTheme(t.id)}
              aria-pressed={theme === t.id}
              aria-label={`切换到${t.name}配色`}
              title={t.name}
              className={`h-8 rounded-md transition-all ${
                theme === t.id
                  ? 'ring-2 ring-neutral-100 ring-offset-2 ring-offset-neutral-950'
                  : 'opacity-70 hover:opacity-100'
              } focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-400/70`}
              style={{ background: themeSwatch(t.id) }}
            />
          ))}
        </div>

        <div className="mt-4 flex items-center justify-between gap-2">
          <span className="text-xs font-medium text-neutral-400 max-sm:hidden">外观</span>
          <ThemeToggle className="max-sm:px-2" />
        </div>
      </div>

      {/* The service keeps sessions in its own memory and exposes no sign-out
          route, so the panel says how a session ends instead of offering a
          button that would only pretend to end it. */}
      <div className="p-4 border-t border-neutral-800 max-sm:hidden">
        <p className="text-xs text-neutral-500 leading-relaxed">
          登录状态保存在服务进程内：服务重启后需要重新登录，关闭浏览器也会结束本次登录。
        </p>
      </div>
    </aside>
  )
}

/** Shared client control for both the authenticated sidebar and the login page. */
export function ThemeToggle({ className = '' }: { className?: string }) {
  const appearance = useThemeStore((state) => state.appearance)
  const setAppearance = useThemeStore((state) => state.setAppearance)

  useEffect(() => {
    let mounted = true

    void Promise.resolve(useThemeStore.persist.rehydrate()).then(() => {
      if (!mounted) return
      const declared = document.documentElement.dataset.appearance
      if (declared === 'light' || declared === 'dark') {
        const current = useThemeStore.getState().appearance
        if (current !== declared) useThemeStore.getState().setAppearance(declared)
      }
    })

    return () => {
      mounted = false
    }
  }, [])

  return (
    <button
      type="button"
      onClick={() => setAppearance(appearance === 'dark' ? 'light' : 'dark')}
      aria-pressed={appearance === 'light'}
      aria-label={`切换到${appearance === 'dark' ? '浅色' : '深色'}模式`}
      title={`切换到${appearance === 'dark' ? '浅色' : '深色'}模式`}
      className={`flex min-w-0 flex-1 items-center justify-center gap-2 rounded-md border border-neutral-800/80 bg-neutral-900/40 px-3 py-2 text-xs font-medium text-neutral-300 transition-colors hover:bg-neutral-800/70 hover:text-neutral-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-neutral-950 ${className}`}
    >
      {appearance === 'dark' ? <Sun size={16} weight="duotone" /> : <Moon size={16} weight="duotone" />}
      <span className="max-sm:hidden">{appearance === 'dark' ? '浅色模式' : '深色模式'}</span>
    </button>
  )
}
