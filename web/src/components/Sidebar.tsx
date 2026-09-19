'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
  House,
  MusicNotes,
  Robot,
  Pulse,
  Article,
  Gear,
  Palette,
  Sliders,
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
    <aside className="w-64 h-screen glass border-r border-neutral-800 flex flex-col">
      {/* Logo */}
      <div className="p-6 border-b border-neutral-800">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-accent-500/20 flex items-center justify-center">
            <MusicNotes size={24} weight="duotone" className="text-accent-400" />
          </div>
          <div>
            <h2 className="font-semibold">tgmusic</h2>
            <p className="text-xs text-neutral-400">管理后台</p>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-4 space-y-1 overflow-y-auto">
        {navItems.map((item) => {
          const isActive = pathname === item.href
          const Icon = item.icon
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-4 py-3 rounded-lg transition-colors ${
                isActive
                  ? 'bg-accent-500/15 text-accent-300'
                  : 'text-neutral-300 hover:bg-neutral-800/50 hover:text-neutral-100'
              }`}
            >
              <Icon size={20} weight={isActive ? 'duotone' : 'regular'} />
              <span className="font-medium text-sm">{item.label}</span>
            </Link>
          )
        })}
      </nav>

      {/* Theme selector */}
      <div className="p-4 border-t border-neutral-800">
        <div className="flex items-center gap-2 mb-3">
          <Palette size={16} className="text-neutral-400" />
          <span className="text-xs font-medium text-neutral-400">配色主题</span>
        </div>
        <div className="grid grid-cols-4 gap-2">
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
              }`}
              style={{ background: themeSwatch(t.id) }}
            />
          ))}
        </div>
      </div>

      {/* The service keeps sessions in its own memory and exposes no sign-out
          route, so the panel says how a session ends instead of offering a
          button that would only pretend to end it. */}
      <div className="p-4 border-t border-neutral-800">
        <p className="text-xs text-neutral-500 leading-relaxed">
          登录状态保存在服务进程内：服务重启后需要重新登录，关闭浏览器也会结束本次登录。
        </p>
      </div>
    </aside>
  )
}
