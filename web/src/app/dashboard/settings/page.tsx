'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Gear, Key, Check, Eye, EyeSlash, ShieldCheck } from '@phosphor-icons/react'
import { USERNAME_STORAGE_KEY, changeCredentials, errorMessage } from '@/lib/api'

export default function SettingsPage() {
  const router = useRouter()
  const [username, setUsername] = useState('admin')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showCurrent, setShowCurrent] = useState(false)
  const [showNew, setShowNew] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [mustChange, setMustChange] = useState(false)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    setMustChange(params.get('must_change') === '1')
    try {
      const remembered = window.localStorage.getItem(USERNAME_STORAGE_KEY)
      if (remembered) setUsername(remembered)
    } catch {
      // Storage disabled; the default stands.
    }
  }, [])

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault()
    setMessage(null)

    if (!username.trim()) {
      setMessage({ type: 'error', text: '用户名不能为空' })
      return
    }
    if (newPassword !== confirmPassword) {
      setMessage({ type: 'error', text: '新密码两次输入不一致' })
      return
    }
    if (newPassword.length < 8) {
      setMessage({ type: 'error', text: '新密码至少需要 8 个字符' })
      return
    }

    setLoading(true)
    try {
      await changeCredentials({ username: username.trim(), password: currentPassword, newPassword })
      try {
        window.localStorage.setItem(USERNAME_STORAGE_KEY, username.trim())
      } catch {
        // Not worth failing a successful change over.
      }
      setMessage({ type: 'success', text: '凭据已更新，当前浏览器已换用新的登录会话' })
      setMustChange(false)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      router.replace('/dashboard/settings')
    } catch (err) {
      setMessage({ type: 'error', text: errorMessage(err) })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight">设置</h1>
        <p className="text-neutral-400 text-sm mt-1">管理管理员账号</p>
      </div>

      <div className="max-w-2xl space-y-6">
        {mustChange && (
          <div className="glass rounded-xl p-4 border-warning/30 flex items-start gap-3">
            <ShieldCheck size={24} weight="fill" className="text-warning flex-shrink-0" />
            <p className="text-sm">
              当前部署仍在使用默认密码，其他接口在修改之前都会拒绝访问。请先在这里设置新的用户名与密码。
            </p>
          </div>
        )}

        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center">
              <Key size={24} weight="duotone" className="text-accent-400" />
            </div>
            <div>
              <h2 className="font-semibold text-lg">修改用户名与密码</h2>
              <p className="text-neutral-400 text-sm">
                服务端要求同时提交用户名、当前密码与新密码；保存后所有旧会话都会失效
              </p>
            </div>
          </div>

          <form onSubmit={handleChangePassword} className="space-y-4">
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
                required
              />
            </div>

            <div>
              <label htmlFor="current-password" className="block text-sm font-medium mb-2">
                当前密码
              </label>
              <div className="relative">
                <input
                  id="current-password"
                  type={showCurrent ? 'text' : 'password'}
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  autoComplete="current-password"
                  className="w-full px-4 py-3 pr-12 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowCurrent(!showCurrent)}
                  aria-label={showCurrent ? '隐藏当前密码' : '显示当前密码'}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-neutral-400 hover:text-neutral-200"
                >
                  {showCurrent ? <EyeSlash size={20} /> : <Eye size={20} />}
                </button>
              </div>
            </div>

            <div>
              <label htmlFor="new-password" className="block text-sm font-medium mb-2">
                新密码
              </label>
              <div className="relative">
                <input
                  id="new-password"
                  type={showNew ? 'text' : 'password'}
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  autoComplete="new-password"
                  className="w-full px-4 py-3 pr-12 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
                  required
                  minLength={8}
                />
                <button
                  type="button"
                  onClick={() => setShowNew(!showNew)}
                  aria-label={showNew ? '隐藏新密码' : '显示新密码'}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-neutral-400 hover:text-neutral-200"
                >
                  {showNew ? <EyeSlash size={20} /> : <Eye size={20} />}
                </button>
              </div>
              <p className="text-neutral-500 text-xs mt-2">至少 8 个字符，且不能是默认密码 password</p>
            </div>

            <div>
              <label htmlFor="confirm-password" className="block text-sm font-medium mb-2">
                确认新密码
              </label>
              <div className="relative">
                <input
                  id="confirm-password"
                  type={showConfirm ? 'text' : 'password'}
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  autoComplete="new-password"
                  className="w-full px-4 py-3 pr-12 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowConfirm(!showConfirm)}
                  aria-label={showConfirm ? '隐藏确认密码' : '显示确认密码'}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-neutral-400 hover:text-neutral-200"
                >
                  {showConfirm ? <EyeSlash size={20} /> : <Eye size={20} />}
                </button>
              </div>
            </div>

            {message && (
              <div
                role="status"
                className={`px-4 py-3 rounded-lg border text-sm ${
                  message.type === 'success'
                    ? 'bg-success/10 border-success/20 text-success'
                    : 'bg-danger/10 border-danger/20 text-danger'
                }`}
              >
                {message.text}
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
                  <Check size={20} weight="bold" />
                  保存修改
                </>
              )}
            </button>
          </form>
        </div>

        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-3 mb-3">
            <Gear size={20} weight="duotone" className="text-neutral-400" />
            <h2 className="font-semibold">关于登录状态</h2>
          </div>
          <p className="text-neutral-400 text-sm leading-relaxed">
            登录会话保存在服务进程的内存里，没有单独的退出接口：关闭浏览器会结束本次登录，服务重启后也需要重新登录。
            修改凭据会让此前发出的所有会话立即失效。
          </p>
        </div>
      </div>
    </div>
  )
}
