'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowClockwise, Article, FunnelSimple, Pause, Play, Terminal } from '@phosphor-icons/react'
import {
  AuditEntry,
  DownloadEvent,
  Page,
  ServiceLogEntry,
  errorMessage,
  mediaUrl,
  readAudit,
  readEvents,
  readServiceLogs,
} from '@/lib/api'
import { formatBytes, shortHash } from '@/lib/format'

type Tab = 'events' | 'audit' | 'service'
type Filter = 'all' | 'failed'
type LogLevel = 'info' | 'warning' | 'error'

const TAB_LABELS: Record<Tab, string> = { events: '下载事件', audit: '管理操作', service: '服务日志' }

/** How many service-log lines the page keeps; the window behind it is the same size. */
const LOG_CAP = 500

/** The service's own lines are the ones an operator is watching, so they poll faster. */
const SERVICE_POLL_MS = 3000

const LEVEL_LABELS: Record<LogLevel, string> = {
  info: '信息及以上',
  warning: '警告及以上',
  error: '错误及以上',
}

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

const LOG_LEVEL_TEXT: Record<string, string> = {
  debug: '调试',
  info: '信息',
  warning: '警告',
  error: '错误',
  critical: '严重',
}

const LOG_LEVEL_CLASS: Record<string, string> = {
  error: 'bg-danger/15 text-danger border-danger/20',
  critical: 'bg-danger/15 text-danger border-danger/20',
  warning: 'bg-warning/15 text-warning border-warning/20',
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

/**
 * The record's own UTC instant, in the operator's clock.
 *
 * The backend stamps every line in UTC so a log window does not have to guess
 * where the service ran; what a human reads has to be local time.
 */
function clockTime(value: string): string {
  const moment = new Date(value)
  if (Number.isNaN(moment.getTime())) return value
  return moment.toLocaleTimeString('zh-CN', { hour12: false })
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

function ServiceLogRow({ entry }: { entry: ServiceLogEntry }) {
  const level = entry.level.toLowerCase()
  return (
    <div className={`px-3 py-2 rounded-lg border text-xs ${LOG_LEVEL_CLASS[level] ?? 'bg-neutral-900/40 border-neutral-800'}`}>
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="text-neutral-500 tabular-nums shrink-0">{clockTime(entry.time)}</span>
        <span className="shrink-0">{LOG_LEVEL_TEXT[level] ?? level}</span>
        <span className="text-neutral-500 font-mono shrink-0 hidden sm:inline">{entry.logger}</span>
        <span className="flex-1 min-w-[12rem] whitespace-pre-wrap break-all text-neutral-200">{entry.message}</span>
      </div>
      {entry.traceback && (
        <pre className="mt-2 text-xs text-neutral-400 whitespace-pre-wrap break-all">{entry.traceback}</pre>
      )}
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
  const [logs, setLogs] = useState<ServiceLogEntry[]>([])
  const [level, setLevel] = useState<LogLevel>('info')
  const [logTotal, setLogTotal] = useState(0)
  const [dropped, setDropped] = useState(0)
  const [paused, setPaused] = useState(false)
  const logsEndRef = useRef<HTMLDivElement>(null)
  // The cursor lives in a ref as well as in state: the polling interval must
  // not be torn down and rebuilt every time one more line arrives.
  const lastIdRef = useRef(0)

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

  /**
   * Pull the service's own log, from the cursor rather than from a page number.
   *
   * `reset` re-reads the newest records and replaces what is shown; the polling
   * path asks only for what arrived after the last id this page holds, so a
   * window that stays open does not reprint itself every three seconds.
   */
  const fetchServiceLogs = useCallback(async (reset: boolean) => {
    const after = reset ? 0 : lastIdRef.current
    try {
      const page = await readServiceLogs(200, after, level)
      lastIdRef.current = page.last_id
      setLogTotal(page.total)
      setDropped(page.dropped)
      setLogs((current) => {
        if (reset) return page.items.slice(-LOG_CAP)
        if (page.items.length === 0) return current
        const merged = [...current, ...page.items]
        return merged.length > LOG_CAP ? merged.slice(merged.length - LOG_CAP) : merged
      })
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [level])

  useEffect(() => {
    void fetchLogs()
    const interval = setInterval(() => void fetchLogs(), 10000)
    return () => clearInterval(interval)
  }, [fetchLogs])

  useEffect(() => {
    if (tab !== 'service' || paused) return
    // The first look at the tab takes the newest records; every one after it
    // asks for what came since, including the load that follows a level change.
    void fetchServiceLogs(lastIdRef.current === 0)
    const interval = setInterval(() => void fetchServiceLogs(false), SERVICE_POLL_MS)
    return () => clearInterval(interval)
  }, [tab, paused, fetchServiceLogs])

  useEffect(() => {
    if (autoScroll && logsEndRef.current) logsEndRef.current.scrollIntoView({ behavior: 'smooth' })
  }, [events, audit, logs, tab, autoScroll])

  const changeLevel = (next: LogLevel) => {
    setLevel(next)
    // A different threshold is a different window, so the cursor starts over.
    lastIdRef.current = 0
    setLogs([])
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const visibleEvents = filter === 'all' ? events : events.filter((event) => event.status !== 'success')
  const visibleAudit = filter === 'all' ? audit : audit.filter((entry) => entry.status !== 'success')
  const empty = tab === 'events' ? visibleEvents.length === 0 : tab === 'audit' ? visibleAudit.length === 0 : logs.length === 0

  return (
    <div className="p-8 flex flex-col h-full">
      <div className="flex items-center justify-between mb-6 gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">日志查看</h1>
          <p className="text-neutral-400 text-sm mt-1">
            服务只把日志留在内存里，重启即清空；下载事件与管理操作每 10 秒刷新，服务日志每 3 秒增量拉取
          </p>
        </div>
        <div className="flex items-center gap-3">
          {tab === 'service' ? (
            <div className="flex items-center gap-2">
              <FunnelSimple size={20} className="text-neutral-400" />
              <select
                value={level}
                onChange={(e) => changeLevel(e.target.value as LogLevel)}
                aria-label="日志级别"
                className="px-3 py-2 rounded-lg bg-neutral-800 border border-neutral-700 text-sm focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
              >
                {(Object.keys(LEVEL_LABELS) as LogLevel[]).map((name) => (
                  <option key={name} value={name}>
                    {LEVEL_LABELS[name]}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <FunnelSimple size={20} className="text-neutral-400" />
              <select
                value={filter}
                onChange={(e) => setFilter(e.target.value as Filter)}
                aria-label="过滤条件"
                className="px-3 py-2 rounded-lg bg-neutral-800 border border-neutral-700 text-sm focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors"
              >
                <option value="all">全部</option>
                <option value="failed">仅失败与异常</option>
              </select>
            </div>
          )}
          <label className="flex items-center gap-2 px-3 py-2 rounded-lg bg-neutral-800 text-sm cursor-pointer">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="w-4 h-4 rounded border-neutral-700 bg-neutral-900 text-accent-500 focus:ring-2 focus:ring-accent-500/20"
            />
            自动滚动
          </label>
          {tab === 'service' && (
            <button
              type="button"
              onClick={() => setPaused((current) => !current)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 font-medium transition-colors"
            >
              {paused ? <Play size={20} /> : <Pause size={20} />}
              {paused ? '继续' : '暂停'}
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              if (tab !== 'service') {
                void fetchLogs()
                return
              }
              // The button also resumes: a paused window that is asked for the
              // newest lines plainly wants to be live again.
              setPaused(false)
              void fetchServiceLogs(true)
            }}
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
              {name === 'events' ? totals.events : name === 'audit' ? totals.audit : logTotal}
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
        {tab === 'service' && (
          <p className="text-xs text-neutral-500 mb-3">
            这里显示的是服务进程自己写入的日志（内存中最多保留最近 500 行，重启即清空），不是容器 stdout 或 Docker 日志。
            {dropped > 0 && ` 已有 ${dropped} 行更早的记录滚出窗口。`}
            {paused && ' 已暂停，点「继续」或「刷新」恢复。'}
          </p>
        )}
        <div className="flex-1 overflow-y-auto space-y-2 font-mono text-sm">
          {empty ? (
            <div className="flex flex-col items-center justify-center h-full text-neutral-500">
              {tab === 'service' ? (
                <>
                  <Terminal size={48} weight="duotone" className="mb-4" />
                  <p>暂无服务日志</p>
                </>
              ) : (
                <>
                  <Article size={48} weight="duotone" className="mb-4" />
                  <p>{filter === 'all' ? '暂无日志' : '没有失败记录'}</p>
                </>
              )}
            </div>
          ) : tab === 'events' ? (
            visibleEvents.map((event, index) => <EventRow key={`${event.request_id}-${event.stage}-${index}`} event={event} />)
          ) : tab === 'audit' ? (
            visibleAudit.map((entry, index) => <AuditRow key={`${entry.action}-${index}`} entry={entry} />)
          ) : (
            logs.map((entry) => <ServiceLogRow key={entry.id} entry={entry} />)
          )}
          <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  )
}
