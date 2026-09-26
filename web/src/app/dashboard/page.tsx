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
      <div className="relative flex min-h-full items-center justify-center overflow-hidden bg-neutral-950">
        <div aria-hidden="true" className="absolute -right-32 -top-32 h-80 w-80 rounded-full bg-accent-500/10 blur-3xl" />
        <div className="relative rounded-2xl border border-neutral-800/80 bg-neutral-900/40 px-6 py-5 text-center shadow-xl shadow-black/10">
          <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-accent-500 border-t-transparent" />
          <p className="mt-3 text-sm text-neutral-400">正在读取服务状态</p>
        </div>
      </div>
    )
  }

  const healthy = health?.status === 'ok'
  const degraded = health?.status === 'degraded'
  const statusTone = degraded
    ? 'border-warning/25 bg-warning/10 text-warning'
    : healthy
      ? 'border-success/25 bg-success/10 text-success'
      : 'border-neutral-800/80 bg-neutral-900/40 text-neutral-400'
  const statusDotTone = degraded ? 'bg-warning' : healthy ? 'bg-success' : 'bg-neutral-500'
  const statusLabel = degraded ? '需要关注' : healthy ? '服务运行正常' : '状态未知'

  return (
    <div className="relative min-h-full overflow-hidden bg-neutral-950">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -right-44 -top-52 h-[30rem] w-[30rem] rounded-full bg-accent-500/10 blur-3xl" />
        <div className="absolute -left-56 top-[34rem] h-[26rem] w-[26rem] rounded-full bg-accent-900/15 blur-3xl" />
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent-400/45 to-transparent" />
      </div>

      <div className="relative mx-auto max-w-7xl space-y-7 p-4 sm:p-6 lg:space-y-8 lg:p-8">
        <header className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-sm font-medium text-accent-300">工作台</p>
            <h1 className="mt-2 text-3xl font-semibold tracking-[-0.03em] text-neutral-50 sm:text-4xl">仪表板</h1>
            <p className="mt-2 text-sm leading-6 text-neutral-400">musicdl 服务概览，关键状态集中在这里。</p>
          </div>
          <div className={`inline-flex w-fit items-center gap-2 rounded-full border px-3 py-2 text-xs font-medium ${statusTone}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${statusDotTone}`} />
            {statusLabel}
          </div>
        </header>

        {error && (
          <p role="alert" className="rounded-2xl border border-danger/25 bg-danger/10 px-4 py-3 text-sm leading-6 text-danger">
            {error}
          </p>
        )}

        <section className="grid grid-cols-1 gap-4 md:grid-cols-3" aria-label="服务概览">
          <div className="glass glass-highlight rounded-2xl p-5 transition-colors hover:border-accent-400/30 sm:p-6">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm text-neutral-400">启用音源</p>
                <p className="mt-3 text-3xl font-semibold tabular-nums tracking-tight text-neutral-50">
                  {sources.enabled}
                  <span className="ml-1 text-base font-normal text-neutral-500">/ {sources.total}</span>
                </p>
                <p className="mt-2 text-xs text-neutral-500">可用于搜索与下载</p>
              </div>
              <div className="rounded-2xl bg-accent-500/15 p-3 text-accent-300 ring-1 ring-accent-300/15">
                <MusicNotes size={22} weight="duotone" aria-hidden="true" />
              </div>
            </div>
          </div>

          <div className="glass glass-highlight rounded-2xl p-5 transition-colors hover:border-accent-400/30 sm:p-6">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm text-neutral-400">启用 Bot</p>
                <p className="mt-3 text-3xl font-semibold tabular-nums tracking-tight text-neutral-50">
                  {bots.enabled}
                  <span className="ml-1 text-base font-normal text-neutral-500">/ {bots.total}</span>
                </p>
                <p className="mt-2 text-xs text-neutral-500">已注册的 Telegram Bot</p>
              </div>
              <div className="rounded-2xl bg-accent-500/15 p-3 text-accent-300 ring-1 ring-accent-300/15">
                <Robot size={22} weight="duotone" aria-hidden="true" />
              </div>
            </div>
          </div>

          <div className="glass glass-highlight rounded-2xl p-5 transition-colors hover:border-accent-400/30 sm:p-6">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm text-neutral-400">依赖状态</p>
                <p
                  className={`mt-3 text-3xl font-semibold tracking-tight ${
                    degraded ? 'text-warning' : healthy ? 'text-success' : 'text-neutral-400'
                  }`}
                >
                  {health?.status ?? '未知'}
                </p>
                <p className="mt-2 text-xs text-neutral-500">主服务、Redis 与 Telegram</p>
              </div>
              <div className="rounded-2xl bg-accent-500/15 p-3 text-accent-300 ring-1 ring-accent-300/15">
                <Pulse size={22} weight="duotone" aria-hidden="true" />
              </div>
            </div>
          </div>
        </section>

        {health && (
          <section className="glass rounded-2xl p-5 sm:p-6" aria-labelledby="dependency-title">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex items-start gap-3">
                <div
                  className={`mt-0.5 rounded-xl p-2 ${
                    degraded
                      ? 'bg-warning/10 text-warning'
                      : healthy
                        ? 'bg-success/10 text-success'
                        : 'bg-neutral-800/60 text-neutral-400'
                  }`}
                >
                  {degraded ? (
                    <WarningCircle size={20} weight="fill" aria-hidden="true" />
                  ) : healthy ? (
                    <CheckCircle size={20} weight="fill" aria-hidden="true" />
                  ) : (
                    <Pulse size={20} weight="duotone" aria-hidden="true" />
                  )}
                </div>
                <div>
                  <h2 id="dependency-title" className="font-semibold text-neutral-100">
                    依赖检查
                  </h2>
                  <p className="mt-1 text-sm leading-6 text-neutral-400">
                    查看支撑搜索与下载流程的服务是否可用。
                  </p>
                </div>
              </div>
              <span className="text-xs text-neutral-500">实时读取</span>
            </div>
            <div className="mt-5 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {(Object.keys(CHECK_LABELS) as (keyof HealthReport['checks'])[]).map((name) => {
                const state = health.checks[name]
                return (
                  <span
                    key={name}
                    className={`flex items-center justify-between gap-3 rounded-xl border px-3 py-2.5 text-sm ${
                      state === 'ok'
                        ? 'border-success/20 bg-success/10 text-success'
                        : state === 'failed' || state === 'unavailable'
                          ? 'border-warning/20 bg-warning/10 text-warning'
                          : 'border-neutral-800/80 bg-neutral-900/40 text-neutral-400'
                    }`}
                  >
                    <span>{CHECK_LABELS[name]}</span>
                    <span className="text-xs opacity-80">{STATE_TEXT[state]}</span>
                  </span>
                )
              })}
            </div>
          </section>
        )}

        <section className="glass glass-highlight rounded-2xl p-5 sm:p-6" aria-labelledby="search-title">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-sm font-medium text-accent-300">开始一个任务</p>
              <h2 id="search-title" className="mt-1 text-xl font-semibold tracking-tight text-neutral-50">
                快速搜索
              </h2>
            </div>
            <p className="max-w-md text-sm leading-6 text-neutral-400 sm:text-right">
              用当前启用的音源搜索歌曲，并试下载到服务端媒体目录。
            </p>
          </div>
          <form onSubmit={handleSearch} className="mt-5 flex flex-col gap-3 sm:flex-row">
            <div className="relative flex-1">
              <MagnifyingGlass
                size={20}
                aria-hidden="true"
                className="absolute left-4 top-1/2 -translate-y-1/2 text-neutral-500"
              />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="输入歌曲名称或艺术家…"
                aria-label="搜索关键词"
                className="w-full rounded-xl border border-neutral-800 bg-neutral-950/45 py-3.5 pl-12 pr-4 text-[15px] text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 hover:border-neutral-700 focus:border-accent-400 focus:ring-4 focus:ring-accent-500/15"
              />
            </div>
            <button
              type="submit"
              disabled={!query.trim()}
              className="min-h-12 rounded-xl bg-accent-500 px-6 py-3 font-medium text-neutral-50 transition-[background-color,box-shadow,transform] hover:bg-accent-400 focus:outline-none focus:ring-4 focus:ring-accent-400/30 active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50"
            >
              搜索
            </button>
          </form>
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-3" aria-label="快捷入口">
          {[
            { href: '/dashboard/sources', title: '音源管理', detail: '导入、启用与删除音源脚本' },
            { href: '/dashboard/bots', title: 'Bot 管理', detail: '注册可搜索的 Telegram Bot' },
            { href: '/dashboard/logs', title: '日志查看', detail: '最近的下载事件与后台操作' },
          ].map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="glass glass-highlight group rounded-2xl p-5 transition-[border-color,background-color,transform] hover:-translate-y-0.5 hover:border-accent-400/30 hover:bg-neutral-800/40 focus:outline-none focus:ring-4 focus:ring-accent-500/20 sm:p-6"
            >
              <div className="flex items-start justify-between gap-5">
                <div>
                  <h3 className="font-semibold text-neutral-100">{link.title}</h3>
                  <p className="mt-2 text-sm leading-6 text-neutral-400">{link.detail}</p>
                </div>
                <ArrowRight
                  size={22}
                  weight="duotone"
                  aria-hidden="true"
                  className="mt-0.5 shrink-0 text-accent-300 transition-transform group-hover:translate-x-1"
                />
              </div>
            </a>
          ))}
        </section>
      </div>
    </div>
  )
}
