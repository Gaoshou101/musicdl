'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  CheckCircle,
  PaperPlaneTilt,
  SignOut,
  TelegramLogo,
  WarningCircle,
} from '@phosphor-icons/react'
import {
  beginTelegramLogin,
  errorMessage,
  logoutTelegram,
  readTelegram,
  submitTelegramPassword,
  TelegramReport,
  verifyTelegramLogin,
} from '@/lib/api'

/**
 * The account the bots are called with.
 *
 * A bot definition says which bot to talk to; it does not authorise the
 * account that does the talking. Filling in api_id and api_hash builds a
 * client, and this card is the one that finishes the sign-in -- a phone
 * number, the code Telegram sends, and a two-step password when the account
 * has one. It stays on the page afterwards because a session can be revoked,
 * and the operator needs to read that here rather than in a failed search.
 */

const STATUS_TEXT: Record<string, string> = {
  ready: '已登录',
  code_required: '等待输入登录码',
  password_required: '需要两步验证密码',
  invalid_session: '未登录',
  rate_limited: '被 Telegram 限流',
  error: '会话出错',
}

const STATUS_CLASS: Record<string, string> = {
  ready: 'bg-success/15 text-success',
  code_required: 'bg-warning/15 text-warning',
  password_required: 'bg-warning/15 text-warning',
  invalid_session: 'bg-neutral-800 text-neutral-400',
  rate_limited: 'bg-danger/15 text-danger',
  error: 'bg-danger/15 text-danger',
}

const INPUT_CLASS =
  'w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors text-sm'

const BUTTON_CLASS =
  'px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center gap-2 text-sm'

type Notice = { tone: 'ok' | 'error'; text: string } | null

export default function TelegramAccountCard() {
  const [report, setReport] = useState<TelegramReport | null>(null)
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)

  const adopt = useCallback((next: TelegramReport) => {
    setReport(next)
    setNotice(null)
  }, [])

  const refresh = useCallback(async () => {
    try {
      adopt(await readTelegram())
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    }
  }, [adopt])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const run = async (action: () => Promise<TelegramReport>, done: string) => {
    setBusy(true)
    setNotice(null)
    try {
      const next = await action()
      setReport(next)
      setCode('')
      setPassword('')
      setNotice({ tone: 'ok', text: done })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    } finally {
      setBusy(false)
    }
  }

  const status = report?.status ?? null
  const badge = STATUS_TEXT[status ?? ''] ?? '未知状态'
  const badgeClass = STATUS_CLASS[status ?? ''] ?? 'bg-neutral-800 text-neutral-400'
  const waiting = status === 'code_required' || Boolean(report?.pending_phone)
  const needsPassword = status === 'password_required'

  return (
    <div className="glass rounded-xl p-6 mb-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-4 min-w-0">
          <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center flex-shrink-0">
            <TelegramLogo size={24} weight="duotone" className="text-accent-400" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="font-semibold">Telegram 账号会话</h2>
              <span className={`text-xs px-2 py-1 rounded-md ${badgeClass}`}>{badge}</span>
              {report && !report.available && (
                <span className="text-xs px-2 py-1 rounded-md bg-warning/15 text-warning">
                  连接器未装配
                </span>
              )}
            </div>
            <p className="text-neutral-400 text-sm mt-1">
              会话档案 <span className="font-mono">{report?.profile ?? 'default'}</span>
              {report?.pending_phone && <> · 等待 <span className="font-mono">{report.pending_phone}</span> 的登录码</>}
            </p>
          </div>
        </div>
        {status === 'ready' && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(() => logoutTelegram(), '已退出登录，下次会重新发码')}
            className="px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 font-medium transition-colors flex items-center gap-2 text-sm flex-shrink-0"
          >
            <SignOut size={18} />
            退出登录
          </button>
        )}
      </div>

      <div className="mt-4 space-y-3">
        {report && !report.enabled && (
          <p className="text-sm text-warning flex items-center gap-2">
            <WarningCircle size={16} />
            运行配置里还没打开 Telegram；打开并保存后立即生效，不用重启。
          </p>
        )}
        {report?.error && (
          <p className="text-sm text-danger flex items-center gap-2">
            <WarningCircle size={16} />
            {report.error}
            {typeof report.retry_after === 'number' && report.retry_after > 0 && (
              <>，请 {report.retry_after} 秒后重试</>
            )}
          </p>
        )}
        {status === 'invalid_session' && (
          <p className="text-sm text-neutral-400">
            还差一次首次登录：填手机号收码，登录成功前所有 Bot 都会返回错误。
          </p>
        )}
        {status === 'ready' && !notice && (
          <p className="text-sm text-success flex items-center gap-2">
            <CheckCircle size={16} />
            账号已授权，Bot 搜索可以调用。
          </p>
        )}

        {needsPassword ? (
          <form
            className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end"
            onSubmit={(e) => {
              e.preventDefault()
              if (!password) return
              void run(() => submitTelegramPassword(password), '两步验证已提交')
            }}
          >
            <div>
              <label htmlFor="telegram-password" className="block text-xs text-neutral-400 mb-1">
                两步验证密码
              </label>
              <input
                id="telegram-password"
                type="password"
                autoComplete="off"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={INPUT_CLASS}
              />
            </div>
            <button type="submit" disabled={busy || !password} className={BUTTON_CLASS}>
              提交
            </button>
          </form>
        ) : waiting ? (
          <form
            className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end"
            onSubmit={(e) => {
              e.preventDefault()
              if (!code) return
              void run(() => verifyTelegramLogin(code), '登录码已提交')
            }}
          >
            <div>
              <label htmlFor="telegram-code" className="block text-xs text-neutral-400 mb-1">
                Telegram 发来的登录码
              </label>
              <input
                id="telegram-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className={INPUT_CLASS}
              />
            </div>
            <button type="submit" disabled={busy || !code} className={BUTTON_CLASS}>
              <CheckCircle size={18} weight="bold" />
              验证并登录
            </button>
          </form>
        ) : (
          <form
            className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end"
            onSubmit={(e) => {
              e.preventDefault()
              if (!phone) return
              void run(() => beginTelegramLogin(phone), '登录码已发送，请查收')
            }}
          >
            <div>
              <label htmlFor="telegram-phone" className="block text-xs text-neutral-400 mb-1">
                手机号（含国家区号，例如 +8613800138000）
              </label>
              <input
                id="telegram-phone"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+8613800138000"
                className={`${INPUT_CLASS} font-mono`}
              />
            </div>
            <button type="submit" disabled={busy || !phone} className={BUTTON_CLASS}>
              <PaperPlaneTilt size={18} weight="bold" />
              发送登录码
            </button>
          </form>
        )}

        {notice && (
          <p
            role="status"
            className={`px-3 py-2 rounded-lg border text-sm ${
              notice.tone === 'ok'
                ? 'bg-success/10 border-success/20 text-success'
                : 'bg-danger/10 border-danger/20 text-danger'
            }`}
          >
            {notice.text}
          </p>
        )}
      </div>
    </div>
  )
}
