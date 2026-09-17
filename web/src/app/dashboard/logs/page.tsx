'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Article, ArrowClockwise, FunnelSimple } from '@phosphor-icons/react'
import {
  AuditEntry,
  DownloadEvent,
  Page,
  errorMessage,
  mediaUrl,
  readAudit,
  readEvents,
} from '@/lib/api'
import { formatBytes, shortHash } from '@/lib/format'

type Tab = 'events' | 'audit'
type Filter = 'all' | 'failed'

const TAB_LABELS: Record<Tab, string> = { events: '下载事件', audit: '管理操作' }

const STATUS_CLASS: Record<string, string> = {
  success: 'bg-success/15 text-success border-success/20',
  failed: 'bg-danger/15 text-danger border-danger/20',
  unavailable: 'bg-warning/15 text-warning border-warning/20',
  rate_limited: 'bg-warning/15 text-warning border-warning/20',
}

const STATUS_TEXT: Record<string, string> = {
  success: '成功',
  failed: '失败',
  unavailable: '不可用',
  rate_limited: '被限流',
}

const STAGE_TEXT: Record<string, string> = {
  download: '下载',
  cleanup: '清理',
  refresh: '重试刷新',
  health: '健康检查',
}

const ACTION_TEXT: Record<string, string> = {
  login: '登录',
  change_credentials: '修改凭据',
  create_source: '导入音源',
  update_source: '修改音源',
  delete_source: '删除音源',
  create_bot: '添加 Bot',
  update_bot: '修改 Bot',
  delete_bot: '删除 Bot',
}

/** The store keeps entries oldest first; a log window wants the newest ones. */
async function readTail<T>(
  fetcher: (offset: number, limit: number) => Promise<Page<T>>,
  limit: number,
): Promise<Page<T>> {
  const head = await fetcher(0, 1)
  if (head.total <= 1) return head
  const offset = Math.max(0, head.total - limit)
  return fetcher(offset, limit)
}

function EventRow({ event }: { event: DownloadEvent }) {
  return (
    <div className={`p-3 rounded-lg border ${STATUS_CLASS[event.status ?? ''] ?? 'bg-neutral-900/40 border-neutral-800'}`}>
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="font-semibold">
          {STAGE_TEXT[event.stage ?? ''] ?? event.stage ?? '事件'} · {STATUS_TEXT[event.status ?? ''] ?? event.status ?? '—'}
        </span>
        <span className="text-neutral-400 font-mono">{event.source_id ?? '—'}</span>
        {event.source_version && <span className="text-neutral-500 font-mono">v{event.source_version}</span>}
        {event.error_code && <span className="text-danger font-mono">{event.error_code}</span>}
      </div>
      <div className="flex items-center gap-3 flex-wrap mt-2 text-xs text-neutral-400">
        <span className="font-mono break-all">候选 {shortHash(event.candidate_id, 16)}</span>
        {typeof event.size_bytes === 'number' && <span>{formatBytes(event.size_bytes)}</span>}
        {event.healthy !== null && event.healthy !== undefined && (
          <span>音源健康 {event.healthy ? '是' : '否'}</span>
        )}
        {event.sha256 && <span className="font-mono">sha256 {shortHash(event.sha256, 12)}</span>}
      </div>
      {event.relative_path && (
        <a
          href={mediaUrl(event.relative_path)}
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-block text-xs font-mono text-accent-300 hover:text-accent-200 break-all"
        >
          {event.relative_path}
        </a>
      )}
    </div>
  )
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  const status = typeof entry.status === 'string' ? entry.status : ''
  const extras = Object.entries(entry)
    .filter(([key]) => !['action', 'status'].includes(key))
    .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : String(value)}`)
    .join('  ')
  return (
    <div className={`p-3 rounded-lg border ${STATUS_CLASS[status] ?? 'bg-neutral-900/40 border-neutral-800'}`}>
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="font-semibold">
          {entry.action ? (ACTION_TEXT[entry.action] ?? entry.action) : '操作'} · {STATUS_TEXT[status] ?? (status || '—')}
        </span>
      </div>
      {extras && <p className="text-xs text-neutral-400 font-mono mt-2 break-all">{extras}</p>}
    </div>
  )
}

export default function LogsPage() {
  const [tab, setTab] = useState<Tab>('events')
  const [filter, setFilter] = useState<Filter>('all')
  const [events, setEvents] = useState<DownloadEvent[]>([])
  const [audit, setAudit] = useState<AuditEntry[]>([])
  const [totals, setTotals] = useState({ events: 0, audit: 0 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [autoScroll, setAutoScroll] = useState(true)
  const logsEndRef = useRef<HTMLDivElement>(null)

  const fetchLogs = useCallback(async () => {
    try {
      const [eventPage, auditPage] = await Promise.all([readTail(readEvents, 100), readTail(readAudit, 100)])
      setEvents(eventPage.items)
      setAudit(auditPage.items)
      setTotals({ events: eventPage.total, audit: auditPage.total })
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchLogs()
    const interval = setInterval(() => void fetchLogs(), 10000)
    return () => clearInterval(interval)
  }, [fetchLogs])

  useEffect(() => {
    if (autoScroll && logsEndRef.current) logsEndRef.current.scrollIntoView({ behavior: 'smooth' })
  }, [events, audit, tab, autoScroll])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const visibleEvents = filter === 'all' ? events : events.filter((event) => event.status !== 'success')
  const visibleAudit = filter === 'all' ? audit : audit.filter((entry) => entry.status !== 'success')
  const empty = tab === 'events' ? visibleEvents.length === 0 : visibleAudit.length === 0

  return (
    <div className="p-8 flex flex-col h-full">
      <div className="flex items-center justify-between mb-6 gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">日志查看</h1>
          <p className="text-neutral-400 text-sm mt-1">
            服务只把日志留在内存里，重启即清空；每 10 秒自动刷新一次
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <FunnelSimple size={20} className="text-neutral-400" />
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as Filter)}
              className="px-3 py-2 rounded-lg bg-neutral-800 border border-neutral-700 text-sm focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
            >
              <option value="all">全部</option>
              <option value="failed">仅失败与异常</option>
            </select>
          </div>
          <label className="flex items-center gap-2 px-3 py-2 rounded-lg bg-neutral-800 text-sm cursor-pointer">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="w-4 h-4 rounded border-neutral-700 bg-neutral-900 text-accent-500 focus:ring-2 focus:ring-accent-500/20"
            />
            自动滚动
          </label>
          <button
            type="button"
            onClick={() => void fetchLogs()}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 font-medium transition-colors"
          >
            <ArrowClockwise size={20} />
            刷新
          </button>
        </div>
      </div>

      <div className="flex items-center gap-2 mb-4">
        {(Object.keys(TAB_LABELS) as Tab[]).map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setTab(name)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              tab === name ? 'bg-accent-500/15 text-accent-300' : 'text-neutral-400 hover:bg-neutral-800/60'
            }`}
          >
            {TAB_LABELS[name]}
            <span className="ml-2 text-xs text-neutral-500 tabular-nums">
              {name === 'events' ? totals.events : totals.audit}
            </span>
          </button>
        ))}
      </div>

      {error && (
        <p role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm mb-4">
          {error}
        </p>
      )}

      <div className="glass rounded-xl p-4 flex-1 overflow-hidden flex flex-col">
        <div className="flex-1 overflow-y-auto space-y-2 font-mono text-sm">
          {empty ? (
            <div className="flex flex-col items-center justify-center h-full text-neutral-500">
              <Article size={48} weight="duotone" className="mb-4" />
              <p>{filter === 'all' ? '暂无日志' : '没有失败记录'}</p>
            </div>
          ) : tab === 'events' ? (
            visibleEvents.map((event, index) => <EventRow key={`${event.request_id}-${event.stage}-${index}`} event={event} />)
          ) : (
            visibleAudit.map((entry, index) => <AuditRow key={`${entry.action}-${index}`} entry={entry} />)
          )}
          <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  )
}
