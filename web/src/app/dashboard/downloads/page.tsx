'use client'

import Link from 'next/link'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowClockwise, ArrowLeft, ArrowRight, ClockCounterClockwise } from '@phosphor-icons/react'
import {
  DownloadHistoryDetail,
  DownloadHistoryEvent,
  DownloadHistoryPage,
  DownloadHistoryStatus,
  downloadErrorMessage,
  errorMessage,
  listDownloadHistory,
  mediaUrl,
  readDownloadHistory,
} from '@/lib/api'
import { formatBytes } from '@/lib/format'
import { usePolling } from '@/lib/usePolling'

const PAGE_SIZE = 20
const STATUS: Record<DownloadHistoryStatus, { label: string; style: string }> = {
  running: { label: '下载中', style: 'text-accent-300 bg-accent-500/10' },
  succeeded: { label: '已完成', style: 'text-success bg-success/10' },
  failed: { label: '失败', style: 'text-danger bg-danger/10' },
  interrupted: { label: '已中断', style: 'text-warning bg-warning/10' },
}
const STAGES: Record<string, string> = {
  resolve: '解析音频地址', transfer: '下载与校验', download_attempt: '尝试下载', download: '下载文件',
  channel_switch: '切换音源', quality: '检查音质', quality_downgrade: '音质降级', quality_downgraded: '音质降级',
  duration: '校验时长', integrity: '校验完整性', cleanup: '清理临时文件',
  refresh: '刷新候选', health: '检查音源', task: '下载任务', recovery: '恢复记录',
}
const EVENT_STATUS: Record<string, string> = {
  started: '开始', running: '进行中', success: '成功', succeeded: '完成',
  failed: '失败', interrupted: '中断', cancelled: '已取消', skipped: '跳过', warning: '提示',
  selected: '已选择', observed: '已记录', unverified: '未验证',
}
const CONTROL = 'inline-flex items-center justify-center gap-2 rounded-lg border border-neutral-700 px-3 py-2 text-sm hover:bg-neutral-800/60 disabled:opacity-40 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-400'

function dateTime(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

function elapsed(value: number | null) {
  if (value === null || !Number.isFinite(value) || value < 0) return '未记录'
  if (value < 1000) return `${Math.round(value)} 毫秒`
  if (value < 60000) return `${(value / 1000).toFixed(1)} 秒`
  return `${Math.floor(value / 60000)} 分 ${Math.floor((value % 60000) / 1000)} 秒`
}

function Status({ value }: { value: DownloadHistoryStatus }) {
  const item = STATUS[value] ?? { label: '状态未知', style: 'text-neutral-400 bg-neutral-800' }
  return <span className={`rounded-md px-2 py-1 text-xs font-medium whitespace-nowrap ${item.style}`}>{item.label}</span>
}

function reasonText(value: string) {
  if (value === 'quality_downgrade') return '当前音源未能提供请求的音质'
  if (value.startsWith('content_error:')) return downloadErrorMessage(value.slice('content_error:'.length))
  return downloadErrorMessage(value)
}

function Attempt({ event }: { event: DownloadHistoryEvent }) {
  return (
    <li className="border-l-2 border-neutral-700 pl-4 py-2 break-words">
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
        <span className="font-medium">{STAGES[event.stage ?? ''] ?? event.stage ?? '事件'} ·{' '}
          <span className={event.status === 'failed' ? 'text-danger' : 'text-neutral-300'}>
            {EVENT_STATUS[event.status ?? ''] ?? event.status ?? '已记录'}
          </span>
        </span>
        <time className="text-xs text-neutral-500" dateTime={event.created_at}>{dateTime(event.created_at)}</time>
      </div>
      <p className="mt-1 text-xs text-neutral-400 break-all">
        {event.from_source_id && event.to_source_id
          ? `${event.from_source_id} → ${event.to_source_id}`
          : event.source_id || '—'}
        {event.elapsed_ms !== null && ` · 耗时 ${elapsed(event.elapsed_ms)}`}
      </p>
      {(event.requested_quality || event.actual_quality) && (
        <p className="mt-1 text-xs text-neutral-400">
          {event.requested_quality && `请求 ${event.requested_quality}`}
          {event.requested_quality && event.actual_quality && ' · '}
          {event.actual_quality && `实际 ${event.actual_quality}`}
        </p>
      )}
      {event.error_code && <p className="text-sm text-warning mt-1">{downloadErrorMessage(event.error_code)}</p>}
      {event.reason && <p className="text-xs text-neutral-400 mt-1">{reasonText(event.reason)}</p>}
    </li>
  )
}

function TaskDetails({ id }: { id: string }) {
  const [detail, setDetail] = useState<DownloadHistoryDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const report = await readDownloadHistory(id, signal)
      if (signal.aborted) return
      setDetail(report)
      setError('')
    } catch (err) {
      if (!signal.aborted) setError(errorMessage(err))
    } finally {
      if (!signal.aborted) setLoading(false)
    }
  }, [id])
  const refresh = usePolling(load, 3000)

  return (
    <section className="glass rounded-xl border border-neutral-800 p-5 min-w-0" aria-label="下载详情">
      <div className="flex items-center justify-between gap-3 mb-4">
        <h2 className="font-semibold">下载详情</h2>
        <button type="button" aria-label="刷新下载详情" className={CONTROL} onClick={() => void refresh()}><ArrowClockwise size={16} /></button>
      </div>
      {error && <p role="alert" className="text-danger text-sm mb-4">{error}</p>}
      {loading && <p role="status" className="text-sm text-neutral-400">正在读取下载详情…</p>}
      {detail && (
        <>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="text-lg font-semibold break-words">{detail.title || '未命名歌曲'}</h3>
              <p className="text-sm text-neutral-400 mt-1 break-words">{detail.artist || '未知艺术家'}</p>
            </div>
            <div role="status"><Status value={detail.status} /></div>
          </div>
          {detail.status === 'interrupted' && (
            <p className="text-sm text-warning mt-4">这次执行或记录已中断，最终结果未确认。重新下载前，请先检查媒体目录。</p>
          )}
          {detail.status === 'running' && <p className="text-sm text-neutral-400 mt-4">正在等待下载结果，此页会自动更新。</p>}
          {detail.error_code && <p className="text-sm text-warning mt-3">{downloadErrorMessage(detail.error_code)}</p>}
          {detail.history_warning && <p className="text-sm text-warning mt-3">历史记录保存不完整，部分过程可能缺失。</p>}
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 mt-5 text-sm">
            <dt className="text-neutral-500">入口</dt><dd>{detail.origin === 'wecom' ? '企业微信' : '管理面板'}</dd>
            <dt className="text-neutral-500">开始时间</dt><dd>{dateTime(detail.created_at)}</dd>
            <dt className="text-neutral-500">{detail.status === 'interrupted' ? '状态确认时间' : '结束时间'}</dt><dd>{dateTime(detail.finished_at)}</dd>
            <dt className="text-neutral-500">总耗时</dt><dd>{elapsed(detail.elapsed_ms)}</dd>
            <dt className="text-neutral-500">请求音质</dt><dd>{detail.requested_quality || '未指定'}</dd>
            <dt className="text-neutral-500">实际音质</dt><dd>{detail.actual_quality || '未确认'}</dd>
            <dt className="text-neutral-500">{detail.result ? '交付音源' : '初始音源'}</dt><dd className="break-all">{detail.result?.source_id || detail.source_id}</dd>
          </dl>
          {detail.result && (
            <div className="mt-5 rounded-lg bg-neutral-900/50 border border-neutral-800 p-3 text-sm">
              <p className="text-neutral-400">{formatBytes(detail.result.size_bytes)} · {detail.result.media_type || '格式未知'}</p>
              {detail.result.relative_path && <a href={mediaUrl(detail.result.relative_path)} target="_blank" rel="noreferrer"
                className="block text-accent-300 hover:underline break-all mt-2">{detail.result.relative_path}</a>}
              <p className="text-xs text-neutral-500 mt-2">保存位置为下载完成时的记录，文件之后可能已被移动或删除。</p>
            </div>
          )}
          <div className="flex flex-wrap gap-3 mt-5">
            <Link href={`/dashboard/search?q=${encodeURIComponent(detail.query || `${detail.title} ${detail.artist}`.trim())}`}
              className="text-sm text-accent-300 hover:underline">重新搜索这首歌 →</Link>
          </div>
          <h3 className="font-medium mt-7 mb-3">下载过程</h3>
          {detail.events.length ? (
            <ol className="space-y-2">{detail.events.map((event, index) => <Attempt key={index} event={event} />)}</ol>
          ) : <p className="text-sm text-neutral-500">暂未记录到来源尝试。</p>}
          {detail.events_truncated && <p className="text-xs text-warning mt-3">过程记录已达到保留上限，部分事件未展示。</p>}
          <p className="text-xs text-neutral-500 mt-5 break-all">记录编号：{detail.id}</p>
        </>
      )}
    </section>
  )
}

export default function DownloadHistoryPageView() {
  const [page, setPage] = useState<DownloadHistoryPage | null>(null)
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => { setSelected(new URLSearchParams(window.location.search).get('id')) }, [])
  const detailsRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (selected && window.matchMedia('(max-width: 1279px)').matches) {
      detailsRef.current?.scrollIntoView({ block: 'start' })
    }
  }, [selected])

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const report = await listDownloadHistory(offset, PAGE_SIZE, signal)
      if (signal.aborted) return
      if (offset > 0 && !report.items.length) {
        setOffset(Math.max(0, Math.ceil(report.total / PAGE_SIZE) - 1) * PAGE_SIZE)
        return
      }
      setPage(report)
      setError('')
    } catch (err) {
      if (!signal.aborted) setError(errorMessage(err))
    } finally {
      if (!signal.aborted) setLoading(false)
    }
  }, [offset])
  const refresh = usePolling(load, 5000)
  const choose = (id: string) => {
    setSelected(id)
    const url = new URL(window.location.href)
    url.searchParams.set('id', id)
    window.history.replaceState(null, '', url)
  }
  const changePage = (next: number) => { setLoading(true); setPage(null); setOffset(next) }

  return (
    <div className="p-4 sm:p-8 max-w-7xl mx-auto">
      <header className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">下载历史</h1>
          <p className="text-sm text-neutral-400 mt-2 leading-relaxed">查看下载结果、音质与换源过程。刷新页面或重启服务后，已保存的记录仍然保留。</p>
          <p className="text-xs text-neutral-500 mt-2">保留最近 1,000 条已结束记录；列表每 5 秒自动更新。</p>
        </div>
        <div className="flex items-center gap-2">
          <Link href="/dashboard/search" className={CONTROL}>搜索音乐</Link>
          <button type="button" className={CONTROL} onClick={() => void refresh()}><ArrowClockwise size={16} />刷新列表</button>
        </div>
      </header>
      {error && <p role="alert" className="text-danger text-sm rounded-lg border border-danger/20 bg-danger/10 p-3 mb-4">{error}</p>}
      <div className={`grid gap-6 items-start ${selected ? 'xl:grid-cols-2' : ''}`}>
        <section aria-label="下载记录" className="min-w-0">
          {loading && <p role="status" className="text-neutral-400 p-6">正在读取下载记录…</p>}
          {!loading && page?.total === 0 && (
            <div className="glass rounded-xl p-10 text-center">
              <ClockCounterClockwise size={40} className="text-neutral-500 mx-auto mb-4" />
              <h2 className="font-semibold">还没有下载记录</h2>
              <p className="text-sm text-neutral-400 mt-2">从面板或企业微信下载歌曲后，可以在这里查看结果。</p>
              <Link href="/dashboard/search" className="inline-block text-accent-300 text-sm mt-5 hover:underline">开始搜索音乐 →</Link>
            </div>
          )}
          {!!page?.items.length && (
            <>
              <div className="flex justify-between gap-3 text-xs text-neutral-500 mb-3">
                <span>共 {page.total} 条记录</span><span>按开始时间从新到旧</span>
              </div>
              <ul className="space-y-3">
                {page.items.map((item) => (
                  <li key={item.id}>
                    <button type="button" onClick={() => choose(item.id)} aria-pressed={selected === item.id}
                      aria-label={`查看 ${item.title || '未命名歌曲'} 的下载记录`}
                      className={`w-full text-left rounded-xl border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-400 ${selected === item.id ? 'border-accent-500 bg-accent-500/10' : 'border-neutral-800 bg-neutral-900/30 hover:border-neutral-600'}`}>
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0"><p className="font-medium truncate">{item.title || '未命名歌曲'}</p><p className="text-sm text-neutral-400 truncate mt-1">{item.artist || '未知艺术家'}</p></div>
                        <Status value={item.status} />
                      </div>
                      <p className="text-xs text-neutral-500 mt-3">{dateTime(item.created_at)} · {item.origin === 'wecom' ? '企业微信' : '管理面板'}</p>
                      <p className="text-xs text-neutral-400 mt-1 break-all">{item.result?.source_id || item.source_id} · 请求 {item.requested_quality || '未指定'} · 实际 {item.actual_quality || '未确认'}</p>
                      {item.error_code && <p className="text-xs text-warning mt-2">{downloadErrorMessage(item.error_code)}</p>}
                    </button>
                  </li>
                ))}
              </ul>
              <nav aria-label="下载历史分页" className="flex items-center justify-between gap-3 mt-5">
                <button type="button" className={CONTROL} disabled={offset === 0 || loading} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}><ArrowLeft size={16} />上一页</button>
                <span className="text-xs text-neutral-500">第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(page.total / PAGE_SIZE))} 页</span>
                <button type="button" className={CONTROL} disabled={offset + PAGE_SIZE >= page.total || loading} onClick={() => changePage(offset + PAGE_SIZE)}>下一页<ArrowRight size={16} /></button>
              </nav>
            </>
          )}
        </section>
        {selected && <div ref={detailsRef} className="order-first xl:order-last min-w-0"><TaskDetails key={selected} id={selected} /></div>}
      </div>
    </div>
  )
}
