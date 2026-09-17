'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  MagnifyingGlass,
  MusicNotes,
  Download,
  CheckCircle,
  XCircle,
  Clock,
} from '@phosphor-icons/react'
import {
  Candidate,
  DownloadReport,
  SearchReport,
  downloadCandidate,
  errorMessage,
  mediaUrl,
  searchCandidates,
} from '@/lib/api'
import { formatBytes, formatDuration, shortHash } from '@/lib/format'

type RowState = { status: 'idle' | 'busy' | 'done' | 'error'; report?: DownloadReport; error?: string }

const rowKey = (candidate: Candidate) => `${candidate.source_id}:${candidate.item_id}`

export default function SearchPage() {
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(20)
  const [searching, setSearching] = useState(false)
  const [report, setReport] = useState<SearchReport | null>(null)
  const [error, setError] = useState('')
  const [sourceFilter, setSourceFilter] = useState('all')
  const [downloads, setDownloads] = useState<Record<string, RowState>>({})

  const runSearch = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return
    setSearching(true)
    setError('')
    setDownloads({})
    try {
      const result = await searchCandidates(trimmed, limit)
      setReport(result)
      setSourceFilter('all')
    } catch (err) {
      setReport(null)
      setError(errorMessage(err))
    } finally {
      setSearching(false)
    }
  }, [limit])

  // The dashboard hands a query over in the URL, so the panel's own search box
  // is not the only way to start one.
  useEffect(() => {
    const handed = new URLSearchParams(window.location.search).get('q')
    if (!handed) return
    setQuery(handed)
    void runSearch(handed)
    // Only on mount: after that the box owns the query.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault()
    await runSearch(query)
  }

  const handleDownload = async (candidate: Candidate) => {
    const key = rowKey(candidate)
    setDownloads((current) => ({ ...current, [key]: { status: 'busy' } }))
    try {
      const result = await downloadCandidate(candidate)
      setDownloads((current) => ({ ...current, [key]: { status: 'done', report: result } }))
    } catch (err) {
      setDownloads((current) => ({ ...current, [key]: { status: 'error', error: errorMessage(err) } }))
    }
  }

  const candidates = report?.candidates ?? []
  const visible = sourceFilter === 'all' ? candidates : candidates.filter((item) => item.source_id === sourceFilter)
  const sourceIds = Array.from(new Set(candidates.map((item) => item.source_id))).sort()

  return (
    <div className="p-8 max-w-5xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-semibold tracking-tight">音乐搜索测试</h1>
        <p className="text-neutral-400 text-sm mt-1">
          用与机器人完全相同的音源注册表搜索并试下载，验证音源是否真的可用
        </p>
      </div>

      <form onSubmit={handleSearch} className="mb-6">
        <div className="flex gap-3">
          <div className="flex-1 relative">
            <MagnifyingGlass size={20} className="absolute left-4 top-1/2 -translate-y-1/2 text-neutral-400" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入歌曲名称或艺术家…"
              className="w-full pl-12 pr-4 py-3 rounded-lg bg-neutral-900 border border-neutral-800 focus:border-accent-500 focus:outline-none transition-colors text-base"
              disabled={searching}
              aria-label="搜索关键词"
            />
          </div>
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            aria-label="结果条数"
            className="px-3 py-3 rounded-lg bg-neutral-900 border border-neutral-800 text-sm focus:border-accent-500 focus:outline-none"
          >
            {[10, 20, 50, 100].map((value) => (
              <option key={value} value={value}>
                {value} 条
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={searching || !query.trim()}
            className="px-6 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center gap-2"
          >
            {searching ? (
              <>
                <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                搜索中
              </>
            ) : (
              <>
                <MagnifyingGlass size={20} weight="bold" />
                搜索
              </>
            )}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm mb-6">
          {error}
        </p>
      )}

      {report && !error && (
        <div className="glass rounded-lg p-4 mb-6">
          <div className="flex items-center gap-3">
            {report.count > 0 ? (
              <CheckCircle size={20} weight="fill" className="text-success flex-shrink-0" />
            ) : (
              <XCircle size={20} weight="fill" className="text-warning flex-shrink-0" />
            )}
            <span className="text-sm">
              「{report.query}」共 {report.total} 个结果，本次展示 {report.count} 条（注册表版本{' '}
              <span className="font-mono">{report.version}</span>）
            </span>
          </div>
          {report.sources.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-3">
              {report.sources.map((status) => (
                <span
                  key={status.id}
                  className={`text-xs px-2 py-1 rounded-md font-mono ${
                    status.status === 'ok'
                      ? 'bg-neutral-800 text-neutral-300'
                      : 'bg-warning/15 text-warning'
                  }`}
                  title={status.status}
                >
                  {status.id}: {status.count}
                  {status.status !== 'ok' ? `（${status.status}）` : ''}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {candidates.length > 0 && (
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">搜索结果</h2>
          {sourceIds.length > 1 && (
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              aria-label="按音源筛选"
              className="px-3 py-2 rounded-lg bg-neutral-900 border border-neutral-800 text-sm focus:border-accent-500 focus:outline-none"
            >
              <option value="all">全部音源</option>
              {sourceIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          )}
        </div>
      )}

      {visible.length > 0 && (
        <div className="space-y-3">
          {visible.map((candidate) => {
            const state = downloads[rowKey(candidate)] ?? { status: 'idle' as const }
            const details = [
              candidate.album,
              candidate.duration ? formatDuration(candidate.duration) : null,
              candidate.bitrate ? `${candidate.bitrate} kbps` : null,
              candidate.format,
              candidate.size ? formatBytes(candidate.size) : null,
            ].filter((value): value is string => Boolean(value))

            return (
              <div key={rowKey(candidate)} className="glass rounded-lg p-4">
                <div className="flex items-center justify-between gap-4">
                  <div className="flex items-center gap-4 flex-1 min-w-0">
                    <div className="w-12 h-12 rounded-lg bg-neutral-800 flex items-center justify-center flex-shrink-0">
                      <MusicNotes size={24} weight="duotone" className="text-neutral-400" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <h4 className="font-medium truncate">{candidate.title}</h4>
                      <div className="flex items-center gap-2 text-neutral-400 text-sm mt-1 flex-wrap">
                        <span className="truncate">{candidate.artist}</span>
                        {details.length > 0 && (
                          <>
                            <span className="text-neutral-600">•</span>
                            <span className="flex items-center gap-1">
                              <Clock size={14} weight="duotone" />
                              {details.join(' · ')}
                            </span>
                          </>
                        )}
                      </div>
                      <p className="text-neutral-500 text-xs mt-1 font-mono">
                        来源 {candidate.source_id} v{candidate.source_version} · {shortHash(candidate.item_id, 20)}
                      </p>
                    </div>
                  </div>

                  <button
                    type="button"
                    onClick={() => void handleDownload(candidate)}
                    disabled={state.status === 'busy'}
                    className="p-2 rounded-lg hover:bg-accent-500/10 text-accent-400 disabled:opacity-50 transition-colors flex-shrink-0"
                    title="下载到服务端媒体目录"
                    aria-label="下载到服务端媒体目录"
                  >
                    {state.status === 'busy' ? (
                      <div className="w-5 h-5 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <Download size={20} weight="duotone" />
                    )}
                  </button>
                </div>

                {state.status === 'error' && (
                  <p className="text-danger text-sm mt-3">下载失败：{state.error}</p>
                )}

                {state.status === 'done' && state.report && (
                  <div className="mt-3 pt-3 border-t border-neutral-800 text-xs text-neutral-400 space-y-1">
                    <p>
                      下载成功：{formatBytes(state.report.size_bytes)} · {state.report.media_type} ·
                      语言 {state.report.language} · 请求 {shortHash(state.report.request_id, 12)}
                    </p>
                    <p className="font-mono">sha256 {state.report.sha256}</p>
                    <a
                      href={mediaUrl(state.report.relative_path)}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-block text-accent-300 hover:text-accent-200 font-mono break-all"
                    >
                      {state.report.relative_path}
                    </a>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {!report && !error && (
        <div className="glass rounded-xl p-12 text-center">
          <MagnifyingGlass size={48} weight="duotone" className="text-neutral-600 mx-auto mb-4" />
          <h3 className="font-semibold mb-2">开始搜索音乐</h3>
          <p className="text-neutral-400 text-sm">
            输入歌曲名称或艺术家；结果里的每一首都来自当前启用的音源
          </p>
        </div>
      )}

      {report && candidates.length === 0 && !error && (
        <div className="glass rounded-xl p-12 text-center">
          <XCircle size={48} weight="duotone" className="text-neutral-600 mx-auto mb-4" />
          <h3 className="font-semibold mb-2">没有找到结果</h3>
          <p className="text-neutral-400 text-sm">
            换个关键词重试；若始终为空，请到「音源管理」确认音源已启用、脚本已安装，再到「健康监控」确认插件执行器正常。
          </p>
        </div>
      )}
    </div>
  )
}
