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
import {
  CheckState,
  HealthReport,
  SourceHealthReport,
  SourceHealthVerdict,
  errorMessage,
  listSourceHealth,
  readHealth,
} from '@/lib/api'

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

const CHANNEL_TEXT: Record<SourceHealthVerdict, string> = {
  ok: '正常',
  degraded: '不稳定',
  failing: '异常',
  unknown: '未验证',
}

const CHANNEL_CLASS: Record<SourceHealthVerdict, string> = {
  ok: 'text-success',
  degraded: 'text-warning',
  failing: 'text-danger',
  unknown: 'text-neutral-400',
}

function ChannelIcon({ status }: { status: SourceHealthVerdict }) {
  if (status === 'ok') return <CheckCircle size={20} weight="fill" className="text-success" />
  if (status === 'failing') return <XCircle size={20} weight="fill" className="text-danger" />
  if (status === 'degraded') return <WarningCircle size={20} weight="fill" className="text-warning" />
  return <Question size={20} weight="fill" className="text-neutral-500" />
}

/** The stage a failure happened in, and the codes the roll-up can carry. */
const STAGE_TEXT: Record<string, string> = {
  search: '搜索',
  download: '下载',
  refresh: '刷新',
  health: '探活',
}

const ERROR_TEXT: Record<string, string> = {
  search_timeout: '搜索超时',
  search_error: '搜索失败',
  search_invalid: '返回内容不合法',
  download_failed: '取不到音频',
  media_timeout: '下载超时',
  health_failed: '探活失败',
  refresh_failed: '刷新失败',
  source_unavailable: '音源不可用',
}

function errorText(code: string): string {
  return ERROR_TEXT[code] ?? code
}

function StateIcon({ state }: { state: CheckState }) {
  if (state === 'ok') return <CheckCircle size={24} weight="fill" className="text-success" />
  if (state === 'failed') return <XCircle size={24} weight="fill" className="text-danger" />
  if (state === 'unavailable') return <WarningCircle size={24} weight="fill" className="text-warning" />
  return <Question size={24} weight="fill" className="text-neutral-500" />
}

export default function HealthPage() {
  const [health, setHealth] = useState<HealthReport | null>(null)
  const [channels, setChannels] = useState<SourceHealthReport | null>(null)
  const [channelsError, setChannelsError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const fetchHealth = useCallback(async () => {
    setRefreshing(true)
    try {
      // The dependency checks and the per-channel roll-up answer different
      // questions, and an operator wants both on the screen they refresh. The
      // roll-up is the newer of the two routes, so a panel that meets an older
      // backend keeps showing the dependency checks and says what is missing
      // rather than failing the whole page.
      const [report, channelReport] = await Promise.all([
        readHealth(),
        listSourceHealth().catch(() => {
          setChannelsError(true)
          return null
        }),
      ])
      setHealth(report)
      setChannels(channelReport)
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

          {channels && (
            <div className="mt-8">
              <h2 className="text-xl font-semibold tracking-tight">渠道健康度</h2>
              <p className="text-neutral-400 text-sm mt-1 mb-4">
                按后台自己发起的搜索与下载汇总：最近 {channels.window} 次结果的成败，以及最后一次报错。没有跑过的渠道显示「未验证」——
                这不是故障，只是还没有证据。到「搜索测试」跑一次，所有启用的音源都会给出一行。
              </p>
              {channels.sources.length === 0 ? (
                <div className="glass rounded-xl p-6 text-sm text-neutral-400">
                  还没有任何音源。先到「音源管理」导入一个脚本，再回到这里跑一次搜索。
                </div>
              ) : (
                <div className="glass rounded-xl divide-y divide-neutral-800/60">
                  {channels.sources.map((channel) => (
                    <div key={channel.id} className="p-4 flex flex-wrap items-center gap-x-6 gap-y-2">
                      <div className="flex items-center gap-3 min-w-0 flex-1">
                        <ChannelIcon status={channel.status} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-sm">{channel.name || channel.id}</span>
                            <span className={`text-xs ${CHANNEL_CLASS[channel.status]}`}>
                              {CHANNEL_TEXT[channel.status]}
                            </span>
                            {!channel.enabled && (
                              <span className="px-2 py-0.5 rounded text-xs bg-neutral-800/60 text-neutral-400">已停用</span>
                            )}
                            {!channel.configured && (
                              <span className="px-2 py-0.5 rounded text-xs bg-neutral-800/60 text-neutral-400">已移除</span>
                            )}
                          </div>
                          <p className="text-neutral-500 font-mono text-xs mt-0.5">{channel.id}</p>
                        </div>
                      </div>
                      <div className="text-xs text-neutral-400 flex flex-wrap gap-x-4 gap-y-1">
                        <span>搜索 {channel.searches}</span>
                        <span>下载 {channel.downloads}</span>
                        <span>
                          成功率 {channel.success_rate === null ? '—' : `${Math.round(channel.success_rate * 100)}%`}
                        </span>
                        {channel.last_health !== null && channel.last_health !== undefined && (
                          <span>探活 {channel.last_health ? '通过' : '未通过'}</span>
                        )}
                      </div>
                      <div className="text-xs text-neutral-500 w-full md:w-60 md:text-right">
                        {channel.last_error ? (
                          <span>
                            最近失败：{STAGE_TEXT[channel.last_error_stage ?? ''] ?? channel.last_error_stage}{' '}
                            {errorText(channel.last_error)}
                          </span>
                        ) : (
                          <span>没有失败记录</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {channelsError && (
            <div className="glass rounded-xl p-5 mt-8 border-warning/30">
              <p className="text-sm text-neutral-300">
                渠道健康度暂时读不到：后端可能还是不支持该接口的版本。上面的依赖检查仍然有效，升级后端后刷新本页即可。
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
