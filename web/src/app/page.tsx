'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { MusicNote, Lock } from '@phosphor-icons/react'
import { USERNAME_STORAGE_KEY, errorMessage, login } from '@/lib/api'

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
    <div className="min-h-[100dvh] flex items-center justify-center p-4">
      {/* Background pattern */}
      <div className="fixed inset-0 -z-10">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,oklch(20%_0.05_240),transparent_50%)]" />
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_70%_60%,oklch(15%_0.03_240),transparent_40%)]" />
      </div>

      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="flex items-center justify-center mb-8">
          <div className="glass glass-highlight rounded-2xl p-4">
            <MusicNote size={40} weight="duotone" className="text-accent-400" />
          </div>
        </div>

        {/* Login card */}
        <div className="glass glass-highlight rounded-2xl p-8">
          <h1 className="text-2xl font-semibold mb-2 text-center">tgmusic 管理后台</h1>
          <p className="text-neutral-400 text-sm mb-6 text-center">Telegram 音乐机器人管理面板</p>

          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <label htmlFor="username" className="block text-sm font-medium mb-2">
                用户名
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
                placeholder="输入用户名"
                required
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium mb-2">
                密码
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
                placeholder="输入密码"
                required
              />
            </div>

            {error && (
              <div role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full px-4 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center justify-center gap-2"
            >
              {loading ? (
                <div className="w-5 h-5 border-2 border-neutral-900 border-t-transparent rounded-full animate-spin" />
              ) : (
                <>
                  <Lock size={20} weight="duotone" />
                  登录
                </>
              )}
            </button>
          </form>
        </div>

        <p className="text-center text-neutral-500 text-sm mt-6">
          使用部署时设置的管理员账号登录；若仍在使用默认密码，登录后会先要求修改。
        </p>
      </div>
    </div>
  )
}
