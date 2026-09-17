'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  Pulse,
  CheckCircle,
  WarningCircle,
  XCircle,
  ArrowClockwise,
  Question,
} from '@phosphor-icons/react'
import { CheckState, HealthReport, errorMessage, readHealth } from '@/lib/api'

type CheckName = keyof HealthReport['checks']

const CHECKS: { name: CheckName; title: string; detail: string }[] = [
  { name: 'readyz', title: '主服务', detail: '消息与下载任务的后台工作进程是否还在运行' },
  { name: 'plugin_runner', title: '插件执行器', detail: '在沙箱里真正抓取音频的执行器' },
  { name: 'redis', title: 'Redis', detail: '缓存、去重与任务队列；未启用企业微信时无需使用' },
  { name: 'telegram', title: 'Telegram', detail: 'Bot 连接与账号会话是否已授权' },
]

const STATE_TEXT: Record<CheckState, string> = {
  ok: '正常',
  failed: '异常',
  unavailable: '不可用',
  not_required: '无需使用',
}

const STATE_CLASS: Record<CheckState, string> = {
  ok: 'text-success',
  failed: 'text-danger',
  unavailable: 'text-warning',
  not_required: 'text-neutral-400',
}

function StateIcon({ state }: { state: CheckState }) {
  if (state === 'ok') return <CheckCircle size={24} weight="fill" className="text-success" />
  if (state === 'failed') return <XCircle size={24} weight="fill" className="text-danger" />
  if (state === 'unavailable') return <WarningCircle size={24} weight="fill" className="text-warning" />
  return <Question size={24} weight="fill" className="text-neutral-500" />
}

export default function HealthPage() {
  const [health, setHealth] = useState<HealthReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const fetchHealth = useCallback(async () => {
    setRefreshing(true)
    try {
      setHealth(await readHealth())
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void fetchHealth()
    const interval = setInterval(() => void fetchHealth(), 30000)
    return () => clearInterval(interval)
  }, [fetchHealth])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const degraded = health?.status === 'degraded'

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">健康监控</h1>
          <p className="text-neutral-400 text-sm mt-1">每 30 秒自动刷新一次</p>
        </div>
        <button
          type="button"
          onClick={() => void fetchHealth()}
          disabled={refreshing}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 font-medium transition-colors"
        >
          <ArrowClockwise size={20} className={refreshing ? 'animate-spin' : ''} />
          刷新
        </button>
      </div>

      {error && (
        <p role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm mb-4">
          {error}
        </p>
      )}

      {health && (
        <>
          <div
            className={`glass rounded-xl p-5 mb-6 flex items-center gap-3 ${
              degraded ? 'border-warning/30' : 'border-success/30'
            }`}
          >
            {degraded ? (
              <WarningCircle size={28} weight="fill" className="text-warning flex-shrink-0" />
            ) : (
              <CheckCircle size={28} weight="fill" className="text-success flex-shrink-0" />
            )}
            <div>
              <p className="font-medium">{degraded ? '存在异常的依赖项' : '所有依赖项正常'}</p>
              <p className="text-neutral-400 text-sm">
                整体状态 <span className={degraded ? 'text-warning' : 'text-success'}>{health.status}</span>
                ：只有「异常」和「不可用」会让整体降级，「无需使用」不算故障。
              </p>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            {CHECKS.map((check) => {
              const state = health.checks[check.name]
              return (
                <div key={check.name} className="glass rounded-xl p-6">
                  <div className="flex items-start justify-between mb-4">
                    <div className="flex items-center gap-3">
                      <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center">
                        <Pulse size={24} weight="duotone" className="text-accent-400" />
                      </div>
                      <div>
                        <h3 className="font-semibold">{check.title}</h3>
                        <p className="text-neutral-400 text-sm">{check.detail}</p>
                      </div>
                    </div>
                    <StateIcon state={state} />
                  </div>
                  <p className="text-sm">
                    <span className="text-neutral-400">状态：</span>
                    <span className={STATE_CLASS[state]}>{STATE_TEXT[state]}</span>
                    <span className="text-neutral-600 font-mono text-xs ml-2">{state}</span>
                  </p>
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
