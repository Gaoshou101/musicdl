'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  MusicNotes,
  Robot,
  Pulse,
  MagnifyingGlass,
  ArrowRight,
  CheckCircle,
  WarningCircle,
} from '@phosphor-icons/react'
import {
  CheckState,
  HealthReport,
  errorMessage,
  listBots,
  listSources,
  readHealth,
} from '@/lib/api'

const CHECK_LABELS: Record<keyof HealthReport['checks'], string> = {
  readyz: '主服务',
  plugin_runner: '插件执行器',
  redis: 'Redis',
  telegram: 'Telegram',
}

const STATE_TEXT: Record<CheckState, string> = {
  ok: '正常',
  failed: '异常',
  unavailable: '不可用',
  not_required: '无需使用',
}

export default function DashboardPage() {
  const router = useRouter()
  const [sources, setSources] = useState({ total: 0, enabled: 0 })
  const [bots, setBots] = useState({ total: 0, enabled: 0 })
  const [health, setHealth] = useState<HealthReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')

  const load = useCallback(async () => {
    try {
      const [sourceReport, botReport, healthReport] = await Promise.all([listSources(), listBots(), readHealth()])
      setSources({
        total: sourceReport.items.length,
        enabled: sourceReport.items.filter((item) => item.enabled).length,
      })
      setBots({
        total: botReport.items.length,
        enabled: botReport.items.filter((item) => item.enabled).length,
      })
      setHealth(healthReport)
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return
    router.push(`/dashboard/search?q=${encodeURIComponent(query.trim())}`)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const degraded = health?.status === 'degraded'

  return (
    <div className="p-8 space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">仪表板</h1>
        <p className="text-neutral-400 text-sm mt-1">tgmusic 服务概览</p>
      </div>

      {error && (
        <p role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm">
          {error}
        </p>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center">
              <MusicNotes size={24} weight="duotone" className="text-accent-400" />
            </div>
            <div>
              <p className="text-neutral-400 text-sm">音源</p>
              <p className="text-2xl font-semibold tabular-nums">
                {sources.enabled}
                <span className="text-neutral-500 text-base font-normal">/{sources.total} 启用</span>
              </p>
            </div>
          </div>
        </div>

        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center">
              <Robot size={24} weight="duotone" className="text-accent-400" />
            </div>
            <div>
              <p className="text-neutral-400 text-sm">Bot</p>
              <p className="text-2xl font-semibold tabular-nums">
                {bots.enabled}
                <span className="text-neutral-500 text-base font-normal">/{bots.total} 启用</span>
              </p>
            </div>
          </div>
        </div>

        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center">
              <Pulse size={24} weight="duotone" className="text-accent-400" />
            </div>
            <div>
              <p className="text-neutral-400 text-sm">依赖状态</p>
              <p className={`text-2xl font-semibold ${degraded ? 'text-warning' : 'text-success'}`}>
                {health?.status ?? '未知'}
              </p>
            </div>
          </div>
        </div>
      </div>

      {health && (
        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-3 mb-4">
            {degraded ? (
              <WarningCircle size={20} weight="fill" className="text-warning" />
            ) : (
              <CheckCircle size={20} weight="fill" className="text-success" />
            )}
            <h2 className="font-semibold">依赖检查</h2>
          </div>
          <div className="flex flex-wrap gap-3">
            {(Object.keys(CHECK_LABELS) as (keyof HealthReport['checks'])[]).map((name) => {
              const state = health.checks[name]
              return (
                <span
                  key={name}
                  className={`text-sm px-3 py-1.5 rounded-lg ${
                    state === 'ok'
                      ? 'bg-success/10 text-success'
                      : state === 'failed' || state === 'unavailable'
                        ? 'bg-warning/10 text-warning'
                        : 'bg-neutral-800 text-neutral-400'
                  }`}
                >
                  {CHECK_LABELS[name]}：{STATE_TEXT[state]}
                </span>
              )
            })}
          </div>
        </div>
      )}

      <div className="glass rounded-xl p-6">
        <h2 className="text-xl font-semibold mb-2">快速搜索</h2>
        <p className="text-neutral-400 text-sm mb-5">
          用当前启用的音源搜索一首歌，并试下载到服务端媒体目录
        </p>
        <form onSubmit={handleSearch} className="flex gap-3">
          <div className="flex-1 relative">
            <MagnifyingGlass size={20} className="absolute left-4 top-1/2 -translate-y-1/2 text-neutral-400" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入歌曲名称或艺术家…"
              aria-label="搜索关键词"
              className="w-full pl-12 pr-4 py-3 rounded-lg bg-neutral-900 border border-neutral-800 focus:border-accent-500 focus:outline-none transition-colors"
            />
          </div>
          <button
            type="submit"
            disabled={!query.trim()}
            className="px-6 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center gap-2"
          >
            搜索
          </button>
        </form>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {[
          { href: '/dashboard/sources', title: '音源管理', detail: '导入、启用与删除音源脚本' },
          { href: '/dashboard/bots', title: 'Bot 管理', detail: '注册可搜索的 Telegram Bot' },
          { href: '/dashboard/logs', title: '日志查看', detail: '最近的下载事件与后台操作' },
        ].map((link) => (
          <a
            key={link.href}
            href={link.href}
            className="glass glass-highlight rounded-xl p-6 hover:bg-neutral-800/50 transition-colors group"
          >
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold mb-1">{link.title}</h3>
                <p className="text-neutral-400 text-sm">{link.detail}</p>
              </div>
              <ArrowRight
                size={22}
                weight="duotone"
                className="text-accent-400 group-hover:translate-x-1 transition-transform"
              />
            </div>
          </a>
        ))}
      </div>
    </div>
  )
}
