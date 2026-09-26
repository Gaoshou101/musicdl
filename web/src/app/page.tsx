'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Lock } from '@phosphor-icons/react'
import { USERNAME_STORAGE_KEY, errorMessage, login } from '@/lib/api'
import { ThemeToggle } from '@/components/Sidebar'

export default function LoginPage() {
  const router = useRouter()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Read after mounting rather than in the initial state: the server renders
  // this form too, and it has no localStorage to read from.
  useEffect(() => {
    try {
      const remembered = window.localStorage.getItem(USERNAME_STORAGE_KEY)
      if (remembered) setUsername(remembered)
    } catch {
      // Storage disabled; the default stands.
    }
  }, [])

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const report = await login(username.trim(), password)
      try {
        window.localStorage.setItem(USERNAME_STORAGE_KEY, username.trim())
      } catch {
        // Not worth failing a successful login over.
      }
      // A deployment still carrying the default password is allowed to reach
      // exactly one page: the credential change. Every other route answers 403.
      router.replace(report.must_change ? '/dashboard/settings?must_change=1' : '/dashboard')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="relative min-h-[100dvh] overflow-hidden bg-neutral-950 text-neutral-100">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -left-32 -top-40 h-[32rem] w-[32rem] rounded-full bg-accent-500/12 blur-3xl" />
        <div className="absolute -bottom-48 -right-40 h-[34rem] w-[34rem] rounded-full bg-accent-900/20 blur-3xl" />
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent-400/50 to-transparent" />
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_0%,transparent_0%,rgb(0_0_0_/_0.08)_70%)]" />
      </div>

      <main className="relative mx-auto flex min-h-[100dvh] w-full max-w-6xl items-center justify-center px-5 py-10 sm:px-8 lg:justify-between lg:gap-16 lg:py-16">
        <section className="hidden max-w-md flex-1 lg:block" aria-label="musicdl 简介">
          <div className="mb-7 flex items-center gap-3">
            <img
              src="/brand/icon.svg"
              alt=""
              aria-hidden="true"
              width={44}
              height={44}
              className="h-11 w-11 rounded-2xl object-cover shadow-sm"
            />
            <div>
              <p className="text-sm font-semibold tracking-wide">musicdl</p>
              <p className="text-xs text-neutral-500">Telegram 音乐服务</p>
            </div>
          </div>

          <h1 className="max-w-sm text-4xl font-semibold leading-[1.12] tracking-[-0.03em] text-neutral-50">
            把音乐服务，<span className="text-accent-300">交给清晰的控制面板。</span>
          </h1>
          <p className="mt-5 max-w-sm text-[15px] leading-7 text-neutral-400">
            管理音源、Telegram Bot 与后台运行状态。登录后，从一个安静、可靠的工作台开始。
          </p>

          <div className="mt-9 grid max-w-sm grid-cols-2 gap-3">
            <div className="rounded-2xl border border-neutral-800/80 bg-neutral-900/35 p-4">
              <p className="text-xs text-neutral-500">音源与 Bot</p>
              <p className="mt-2 text-sm font-medium text-neutral-200">集中管理</p>
            </div>
            <div className="rounded-2xl border border-neutral-800/80 bg-neutral-900/35 p-4">
              <p className="text-xs text-neutral-500">运行依赖</p>
              <p className="mt-2 text-sm font-medium text-neutral-200">实时可见</p>
            </div>
          </div>
        </section>

        <section className="w-full max-w-md" aria-label="管理员登录">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 lg:hidden">
              <img
                src="/brand/icon.svg"
                alt=""
                aria-hidden="true"
                width={40}
                height={40}
                className="h-10 w-10 rounded-xl object-cover shadow-sm"
              />
              <div>
                <p className="text-sm font-semibold">musicdl</p>
                <p className="text-xs text-neutral-500">管理后台</p>
              </div>
            </div>
            <ThemeToggle className="ml-auto min-h-10 flex-none rounded-xl px-3 py-2 focus-visible:ring-4 focus-visible:ring-accent-500/20" />
          </div>

          <div className="glass glass-highlight relative overflow-hidden rounded-[1.75rem] p-6 shadow-2xl shadow-black/20 sm:p-8">
            <div className="absolute inset-x-8 top-0 h-px bg-gradient-to-r from-transparent via-accent-300/70 to-transparent" />
            <div className="mb-8 flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-accent-300">管理员入口</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight text-neutral-50">欢迎回来</h2>
                <p className="mt-2 text-sm leading-6 text-neutral-400">登录以查看服务状态与管理设置。</p>
              </div>
              <div className="rounded-xl bg-neutral-900/50 p-2.5 text-accent-300 ring-1 ring-neutral-800/80">
                <Lock size={19} weight="duotone" aria-hidden="true" />
              </div>
            </div>

            <form onSubmit={handleLogin} className="space-y-5">
              <div>
                <label htmlFor="username" className="mb-2 block text-sm font-medium text-neutral-200">
                  用户名
                </label>
                <input
                  id="username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  className="w-full rounded-xl border border-neutral-800 bg-neutral-950/45 px-4 py-3.5 text-[15px] text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 hover:border-neutral-700 focus:border-accent-400 focus:ring-4 focus:ring-accent-500/15"
                  placeholder="输入用户名"
                  required
                />
              </div>

              <div>
                <label htmlFor="password" className="mb-2 block text-sm font-medium text-neutral-200">
                  密码
                </label>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  className="w-full rounded-xl border border-neutral-800 bg-neutral-950/45 px-4 py-3.5 text-[15px] text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 hover:border-neutral-700 focus:border-accent-400 focus:ring-4 focus:ring-accent-500/15"
                  placeholder="输入密码"
                  required
                />
              </div>

              {error && (
                <div
                  role="alert"
                  className="rounded-xl border border-danger/25 bg-danger/10 px-4 py-3 text-sm leading-6 text-danger"
                >
                  {error}
                </div>
              )}

              <button
                type="submit"
                disabled={loading}
                className="flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-accent-500 px-4 py-3 font-medium text-neutral-50 shadow-lg shadow-accent-500/15 transition-[background-color,box-shadow,transform] hover:bg-accent-400 hover:shadow-accent-500/25 focus:outline-none focus:ring-4 focus:ring-accent-400/30 active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none"
              >
                {loading ? (
                  <div className="h-5 w-5 animate-spin rounded-full border-2 border-neutral-50 border-t-transparent" />
                ) : (
                  <>
                    <Lock size={19} weight="duotone" aria-hidden="true" />
                    登录
                  </>
                )}
              </button>
            </form>

            <div className="mt-7 flex items-start gap-2 border-t border-neutral-800/70 pt-5">
              <p className="text-xs leading-5 text-neutral-500">
                使用部署时设置的管理员账号登录；若仍在使用默认密码，登录后会先要求修改。
              </p>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
