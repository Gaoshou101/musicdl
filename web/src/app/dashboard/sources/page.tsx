'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  MusicNote,
  Plus,
  Trash,
  PencilSimple,
  Check,
  X,
  UploadSimple,
  LinkSimple,
  SpinnerGap,
  FileCode,
  CheckCircle,
  WarningCircle,
  Power,
} from '@phosphor-icons/react'
import {
  SourceHealthRow,
  SourceHealthVerdict,
  analyzeSource,
  fetchSource,
  deleteSource,
  errorMessage,
  installSource,
  listSourceHealth,
  listSources,
  reloadNote,
  updateSource,
} from '@/lib/api'
import type { SourceItem } from '@/lib/api'
import { formatBytes } from '@/lib/format'
import {
  cloneImportGrants,
  duplicateSourceIds,
  hasTooManySourceUrls,
  isSupportedSourceFilename,
  importRowIsInstallable,
  installSourceQueueSequentially,
  NO_IMPORT_GRANTS,
  parseSourceUrlLines,
  requiredGrantKeys,
  sourceImportDraft,
  SOURCE_IMPORT_MAX_BYTES,
  SOURCE_IMPORT_MAX_URLS,
  sourceLanguageForFilename,
} from '@/lib/sourceImport'
import type { ImportGrantState, SourceImportRow } from '@/lib/sourceImport'

/** The egress widenings a preview can ask the operator to grant, in one order. */
const GRANTS = ['allow_insecure_http', 'allow_ip_hosts', 'allow_any_host', 'allowed_ports'] as const

const GRANT_LABELS: Record<string, { title: string; detail: string }> = {
  allow_insecure_http: {
    title: '允许明文 http://',
    detail: '脚本会以未加密的方式请求音频地址，流量在网络上可被读取。',
  },
  allow_ip_hosts: {
    title: '允许直接使用 IP 地址',
    detail: '脚本请求的是 IP 而不是域名，无法用域名证书校验对端身份。',
  },
  allow_any_host: {
    title: '允许访问任意域名',
    detail: '不再按脚本声明的域名限制出口，这是范围最大的一项授权。',
  },
  allowed_ports: {
    title: '允许使用非常规端口',
    detail: '按脚本自己声明的端口精确授权，不会开放其他端口。',
  },
}

type GrantState = ImportGrantState

const NO_GRANTS: GrantState = NO_IMPORT_GRANTS

const BATCH_STATUS_TEXT: Record<SourceImportRow['status'], string> = {
  fetching: '获取中…',
  analyzing: '分析中…',
  ready: '待安装',
  installing: '安装中…',
  installed: '已安装',
  failed: '失败',
}

type AnalysisSummary = {
  name?: string | null
  version?: string | null
  author?: string | null
  homepage?: string | null
  description?: string | null
  sha256?: string | null
  size_bytes?: number | null
  verdict?: string | null
  referenced_hosts?: string[]
  allowed_hosts?: string[]
  schemes?: string[]
  allowed_ports?: number[]
  open_egress?: boolean
  opaque?: boolean
  blockers?: { code: string; detail: string }[]
  caveats?: { code: string; detail: string }[]
}

type Notice = { tone: 'ok' | 'error'; text: string } | null

/** The verdict the health roll-up reaches for one channel, as a badge. */
const VERDICT_TEXT: Record<SourceHealthVerdict, string> = {
  ok: '正常',
  degraded: '不稳定',
  failing: '异常',
  unknown: '未验证',
}

const VERDICT_CLASS: Record<SourceHealthVerdict, string> = {
  ok: 'bg-success/15 text-success',
  degraded: 'bg-warning/15 text-warning',
  failing: 'bg-danger/15 text-danger',
  unknown: 'bg-neutral-800 text-neutral-500',
}

/**
 * The health roll-up's verdict for one row.
 *
 * A component rather than an inline expression because the map has no record
 * for a source that has never been exercised, and the badge has to disappear
 * rather than advertise a verdict the roll-up never reached.
 */
function ChannelBadge({ row }: { row: SourceHealthRow | undefined }) {
  if (!row) return null
  const rate = row.success_rate === null ? '—' : `${Math.round(row.success_rate * 100)}%`
  return (
    <span
      className={`text-xs px-2 py-1 rounded-md ${VERDICT_CLASS[row.status]}`}
      title={`最近 ${row.attempts} 次结果的成功率 ${rate}`}
    >
      渠道 {VERDICT_TEXT[row.status]}
    </span>
  )
}

function EgressLine({ source }: { source: SourceItem }) {
  // The stored script is the only authority on what this source may reach, so
  // the row reads the widenings back rather than restating what was requested.
  const egress = source.plugin?.egress
  if (!egress) return <span className="text-neutral-500">脚本尚未安装，出口策略未知</span>
  const bits = [
    `${egress.allowed_hosts?.length ?? 0} 个域名`,
    `端口 ${(egress.allowed_ports?.length ? egress.allowed_ports : [443]).join('/')}`,
  ]
  if (egress.allow_insecure_http) bits.push('允许明文')
  if (egress.allow_ip_hosts) bits.push('允许 IP')
  if (egress.allow_any_host) bits.push('允许任意域名')
  return <span className="text-neutral-400">{bits.join(' · ')}</span>
}

function ImportDialog({
  onClose,
  onBatchChanged,
  existingIds,
}: {
  onClose: () => void
  onBatchChanged: () => void
  existingIds: ReadonlySet<string>
}) {
  const [error, setError] = useState('')
  const [urlText, setUrlText] = useState('')
  const [batch, setBatch] = useState<SourceImportRow[]>([])
  const [batchBusy, setBatchBusy] = useState(false)
  const analysisTokens = useRef<Record<string, number>>({})
  const batchGeneration = useRef(0)

  const updateBatchRow = (key: string, update: (row: SourceImportRow) => SourceImportRow) => {
    setBatch((current) => current.map((row) => (row.key === key ? update(row) : row)))
  }

  const analyzeBatchRow = async (candidate: SourceImportRow) => {
    const token = (analysisTokens.current[candidate.key] ?? 0) + 1
    analysisTokens.current[candidate.key] = token
    updateBatchRow(candidate.key, (row) => ({ ...row, status: 'analyzing', error: null, preview: null }))
    try {
      const draft = sourceImportDraft(candidate)
      const first = await analyzeSource(draft)
      if (analysisTokens.current[candidate.key] !== token) return
      const suggested = candidate.id.trim() || first.id.suggested
      const final = !candidate.id.trim() && suggested
        ? await analyzeSource({ ...draft, id: suggested })
        : first
      if (analysisTokens.current[candidate.key] !== token) return
      updateBatchRow(candidate.key, (row) => ({
        ...row,
        id: suggested,
        preview: final,
        status: 'ready',
        error: null,
        existing: existingIds.has(suggested),
      }))
    } catch (err) {
      if (analysisTokens.current[candidate.key] !== token) return
      updateBatchRow(candidate.key, (row) => ({ ...row, status: 'failed', error: errorMessage(err) }))
    }
  }

  const analyzeFetchedRow = async (
    empty: SourceImportRow,
    fetched: { script: string; filename: string; language: 'javascript' | 'python' },
  ) => {
    const candidate: SourceImportRow = {
      ...empty,
      script: fetched.script,
      filename: fetched.filename,
      language: fetched.language,
      status: 'analyzing',
    }
    updateBatchRow(empty.key, () => candidate)
    await analyzeBatchRow(candidate)
  }

  const handleBatchUrls = async () => {
    if (batchBusy) return
    const remaining = SOURCE_IMPORT_MAX_URLS - batch.length
    if (hasTooManySourceUrls(urlText, remaining)) {
      setError(`导入队列最多保留 ${SOURCE_IMPORT_MAX_URLS} 项，请先安装或清理已有项目。`)
      return
    }
    const urls = parseSourceUrlLines(urlText, remaining)
    if (!urls.length) {
      setError('请先输入音源 URL，每行一个。')
      return
    }
    const generation = batchGeneration.current + 1
    batchGeneration.current = generation
    setBatchBusy(true)
    setError('')
    setUrlText('')
    for (const [index, url] of urls.entries()) {
      if (batchGeneration.current !== generation) break
      const key = 'url-' + generation + '-' + index
      const empty: SourceImportRow = {
        key,
        url,
        script: '',
        filename: '',
        language: sourceLanguageForFilename(url),
        id: '',
        grants: cloneImportGrants(NO_GRANTS),
        preview: null,
        status: 'fetching',
        error: null,
        result: null,
        reload: null,
        existing: false,
      }
      setBatch((current) => [...current, empty])
      try {
        const fetched = await fetchSource(url)
        if (batchGeneration.current !== generation) break
        await analyzeFetchedRow(empty, fetched)
      } catch (err) {
        if (batchGeneration.current !== generation) break
        updateBatchRow(key, (row) => ({ ...row, status: 'failed', error: errorMessage(err) }))
      }
    }
    if (batchGeneration.current === generation) setBatchBusy(false)
  }

  const handleFiles = async (fileList: FileList | null) => {
    const files = Array.from(fileList ?? [])
    if (!files.length || batchBusy) return
    const remaining = SOURCE_IMPORT_MAX_URLS - batch.length
    if (files.length > remaining) {
      setError(`导入队列最多保留 ${SOURCE_IMPORT_MAX_URLS} 项，请先安装或清理已有项目。`)
      return
    }
    const generation = batchGeneration.current + 1
    batchGeneration.current = generation
    setBatchBusy(true)
    setError('')
    for (const [index, file] of files.entries()) {
      if (batchGeneration.current !== generation) break
      const key = 'file-' + generation + '-' + index
      const empty: SourceImportRow = {
        key,
        url: file.name,
        file,
        script: '',
        filename: file.name,
        language: sourceLanguageForFilename(file.name),
        id: '',
        grants: cloneImportGrants(NO_GRANTS),
        preview: null,
        status: 'fetching',
        error: null,
        result: null,
        reload: null,
        existing: false,
      }
      setBatch((current) => [...current, empty])
      try {
        if (!isSupportedSourceFilename(file.name)) {
          throw new Error('只支持 .js、.mjs、.cjs 和 .py 文件。')
        }
        if (file.size > SOURCE_IMPORT_MAX_BYTES) {
          throw new Error(`脚本超过 ${formatBytes(SOURCE_IMPORT_MAX_BYTES)} 限制，请缩小后再导入。`)
        }
        const script = await file.text()
        if (batchGeneration.current !== generation) break
        await analyzeFetchedRow(empty, {
          script,
          filename: file.name,
          language: sourceLanguageForFilename(file.name),
        })
      } catch (err) {
        if (batchGeneration.current !== generation) break
        updateBatchRow(key, (row) => ({
          ...row,
          status: 'failed',
          error: err instanceof Error ? err.message : errorMessage(err),
        }))
      }
    }
    if (batchGeneration.current === generation) setBatchBusy(false)
  }

  const editBatchRow = (row: SourceImportRow, changes: Partial<SourceImportRow>) => {
    const next: SourceImportRow = {
      ...row,
      ...changes,
      preview: null,
      status: 'analyzing',
      error: null,
      result: null,
      reload: null,
      existing: existingIds.has(String(changes.id ?? row.id).trim()),
    }
    updateBatchRow(row.key, () => next)
    void analyzeBatchRow(next)
  }

  const retryBatchRow = async (row: SourceImportRow) => {
    if (batchBusy || row.status === 'installing') return
    updateBatchRow(row.key, (current) => ({
      ...current,
      status: 'fetching',
      error: null,
      preview: null,
      result: null,
      reload: null,
    }))
    try {
      const fetched = row.file
        ? {
            script: await row.file.text(),
            filename: row.file.name,
            language: sourceLanguageForFilename(row.file.name),
          }
        : await fetchSource(row.url)
      await analyzeFetchedRow({
        ...row,
        error: null,
        preview: null,
        result: null,
        reload: null,
      }, fetched)
    } catch (err) {
      updateBatchRow(row.key, (current) => ({ ...current, status: 'failed', error: errorMessage(err) }))
    }
  }

  const toggleBatchGrant = (row: SourceImportRow, key: string) => {
    const grants = cloneImportGrants(row.grants)
    if (key === 'allowed_ports') {
      const ports = row.preview?.analysis && typeof row.preview.analysis === 'object'
        ? (row.preview.analysis as AnalysisSummary).allowed_ports
        : null
      grants.allowed_ports = grants.allowed_ports ? null : (ports?.length ? ports : [443])
    } else {
      const grant = key as keyof Omit<GrantState, 'allowed_ports'>
      grants[grant] = !grants[grant]
    }
    editBatchRow(row, { grants })
  }

  const handleBatchInstall = async () => {
    if (batchBusy) return
    const duplicateIds = duplicateSourceIds(batch)
    const ready = batch.filter((row) => importRowIsInstallable(row, duplicateIds) && !row.result)
    if (!ready.length) {
      setError(duplicateIds.size ? '存在重复的音源 ID，请先修改后再安装。' : '没有可安装的音源，请先完成获取与预览。')
      return
    }
    setBatchBusy(true)
    setError('')
    await installSourceQueueSequentially(
      batch,
      (source) => installSource(source),
      (row, patch) => {
        updateBatchRow(row.key, (current) => {
          const next = { ...current, ...patch }
          if (patch.status === 'installed' && patch.result) {
            next.reload = reloadNote(patch.result.reload) || null
          }
          return next
        })
        if (patch.status === 'installed') onBatchChanged()
      },
      errorMessage,
    )
    setBatchBusy(false)
  }

  const batchDuplicates = duplicateSourceIds(batch)
  const batchReadyCount = batch.filter((row) => importRowIsInstallable(row, batchDuplicates)).length
  const batchCompletedCount = batch.filter((row) => row.status === 'installed').length
  const importBusy = batchBusy

  return (
    <div className="fixed inset-0 bg-neutral-950/80 backdrop-blur-sm flex items-start justify-center p-4 z-50 overflow-y-auto">
      <div className="glass glass-highlight rounded-2xl p-6 w-full max-w-2xl my-8">
        <h2 className="text-xl font-semibold mb-1">导入音源脚本</h2>
        <p className="text-neutral-400 text-sm mb-5">
          可选择本地脚本或粘贴 URL 批量加入队列；服务会逐个解析身份与出口授权，确认后再写入。
        </p>

        <label className="block mb-4">
          <div className="border-2 border-dashed border-neutral-800 rounded-xl p-6 text-center hover:border-accent-500/50 transition-colors cursor-pointer">
            <input
              type="file"
              accept=".js,.mjs,.cjs,.py"
              multiple
              className="hidden"
              disabled={importBusy}
              onChange={(e) => {
                void handleFiles(e.target.files)
                e.currentTarget.value = ''
              }}
            />
            <UploadSimple size={40} weight="duotone" className="text-neutral-600 mx-auto mb-3" />
            <p className="font-medium mb-1">选择 JS / PY 文件（可多选）</p>
            <p className="text-neutral-400 text-sm">支持 lx-music 音源脚本与普通 JS / Python 脚本，单个文件不超过 256 KiB</p>
          </div>
        </label>

        <section className="border-t border-neutral-800 pt-5 mb-5">
          <div className="flex items-start gap-3 mb-3">
            <LinkSimple size={24} weight="duotone" className="text-accent-400 flex-shrink-0 mt-0.5" />
            <div>
              <h3 className="font-medium">从 URL 批量获取</h3>
              <p className="text-neutral-400 text-sm mt-1">
                每行输入一个音源脚本 URL，服务会按顺序获取并逐个分析，最多 {SOURCE_IMPORT_MAX_URLS} 个。
              </p>
            </div>
          </div>
          <textarea
            value={urlText}
            onChange={(e) => setUrlText(e.target.value)}
            disabled={importBusy}
            rows={4}
            placeholder={'https://example.com/source.js\nhttps://example.org/source.py'}
            className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors font-mono text-sm resize-y disabled:opacity-50"
          />
          <div className="flex flex-wrap items-center justify-between gap-3 mt-3">
            <p className="text-neutral-500 text-xs">仅支持受安全策略保护的 HTTP(S) 地址，单个脚本不超过 256 KiB。</p>
            <button
              type="button"
              onClick={() => void handleBatchUrls()}
              disabled={importBusy || !urlText.trim()}
              className="px-3 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center gap-2"
            >
              {batchBusy ? <SpinnerGap size={18} className="animate-spin" /> : <LinkSimple size={18} />}
              获取并预览
            </button>
          </div>
        </section>

        {batch.length > 0 && (
          <section className="space-y-3 mb-5" aria-label="批量导入队列">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="font-medium">导入队列</h3>
                <p className="text-neutral-500 text-xs mt-1">
                  已安装 {batchCompletedCount}/{batch.length} · 当前可安装 {batchReadyCount}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void handleBatchInstall()}
                disabled={importBusy || batchReadyCount === 0}
                className="px-3 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center gap-2"
              >
                {batchBusy ? <SpinnerGap size={18} className="animate-spin" /> : <Check size={18} weight="bold" />}
                安装可用音源
              </button>
            </div>

            {batch.map((row, index) => {
              const rowAnalysis = (row.preview?.analysis ?? {}) as AnalysisSummary
              const rowRequiredGrants = requiredGrantKeys(row.preview).filter((key) =>
                GRANTS.includes(key as (typeof GRANTS)[number]),
              )
              const rowDuplicate = batchDuplicates.has(row.id.trim())
              const rowBusy = row.status === 'fetching' || row.status === 'analyzing' ||
                row.status === 'installing'
              const rowStatusClass = row.status === 'failed'
                ? 'bg-danger/15 text-danger'
                : row.status === 'installed'
                  ? 'bg-success/15 text-success'
                  : row.status === 'ready'
                    ? 'bg-accent-500/15 text-accent-300'
                    : 'bg-neutral-800 text-neutral-400'
              return (
                <div key={row.key} className="glass rounded-xl p-4 space-y-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-3 min-w-0">
                      <span className="w-6 h-6 rounded-md bg-neutral-800 text-neutral-400 text-xs flex items-center justify-center flex-shrink-0">
                        {index + 1}
                      </span>
                      <div className="min-w-0">
                        <p className="font-medium truncate">{row.filename || '待获取文件名'}</p>
                        <p className="text-neutral-500 text-xs font-mono truncate" title={row.url}>{row.url}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <span className={`text-xs px-2 py-1 rounded-md ${rowStatusClass}`}>
                        {BATCH_STATUS_TEXT[row.status]}
                      </span>
                      {row.status === 'failed' && (
                        <button
                          type="button"
                          onClick={() => void retryBatchRow(row)}
                          disabled={importBusy}
                          className="text-xs px-2 py-1 rounded-md bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 transition-colors"
                        >
                          重试
                        </button>
                      )}
                    </div>
                  </div>

                  {row.error && <p role="alert" className="text-danger text-sm">{row.error}</p>}

                  {(row.script || row.status === 'ready' || row.status === 'installed') && (
                    <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_9rem]">
                      <div>
                        <label htmlFor={`batch-id-${row.key}`} className="block text-xs text-neutral-400 mb-1">
                          音源 ID
                        </label>
                        <input
                          id={`batch-id-${row.key}`}
                          value={row.id}
                          disabled={rowBusy || row.status === 'installed'}
                          onChange={(e) => {
                            const id = e.target.value
                            updateBatchRow(row.key, (current) => ({
                              ...current,
                              id,
                              preview: null,
                              error: null,
                              existing: existingIds.has(id.trim()),
                            }))
                          }}
                          onBlur={() => {
                            const current = batch.find((candidate) => candidate.key === row.key)
                            if (current && current.id.trim() !== row.id.trim()) void editBatchRow(current, { id: current.id })
                            else if (current && !current.preview) void editBatchRow(current, { id: current.id })
                          }}
                          className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm font-mono disabled:opacity-50"
                        />
                      </div>
                      <div>
                        <label htmlFor={`batch-language-${row.key}`} className="block text-xs text-neutral-400 mb-1">
                          脚本语言
                        </label>
                        <select
                          id={`batch-language-${row.key}`}
                          value={row.language}
                          disabled={rowBusy || row.status === 'installed'}
                          onChange={(e) => void editBatchRow(row, {
                            language: e.target.value as 'javascript' | 'python',
                          })}
                          className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm disabled:opacity-50"
                        >
                          <option value="javascript">javascript</option>
                          <option value="python">python</option>
                        </select>
                      </div>
                    </div>
                  )}

                  {row.preview && (
                    <div className="border-t border-neutral-800 pt-3 space-y-3">
                      <div className="flex items-start gap-2">
                        {row.preview.installable ? (
                          <CheckCircle size={20} weight="fill" className="text-success flex-shrink-0" />
                        ) : (
                          <WarningCircle size={20} weight="fill" className="text-warning flex-shrink-0" />
                        )}
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-medium">
                            {rowAnalysis.name || row.id || row.filename || '未命名音源'}
                            {rowAnalysis.version ? <span className="text-neutral-400 font-normal"> v{rowAnalysis.version}</span> : null}
                          </p>
                          <p className="text-neutral-400 text-xs mt-1">
                            {row.preview.installable ? '可安装' : '还需要处理'} · {row.preview.install_path === 'lx' ? 'lx 自定义音源' : '通用脚本'}
                          </p>
                        </div>
                        {row.existing && (
                          <span className="text-xs px-2 py-1 rounded-md bg-warning/15 text-warning whitespace-nowrap">
                            将替换现有版本
                          </span>
                        )}
                        {rowDuplicate && (
                          <span className="text-xs px-2 py-1 rounded-md bg-danger/15 text-danger whitespace-nowrap">
                            ID 重复
                          </span>
                        )}
                      </div>

                      {(rowAnalysis.blockers ?? []).map((finding) => (
                        <p key={finding.code} className="text-danger text-xs">拒绝原因 {finding.code}：{finding.detail}</p>
                      ))}
                      {(rowAnalysis.caveats ?? []).map((finding) => (
                        <p key={finding.code} className="text-neutral-400 text-xs">提示 {finding.code}：{finding.detail}</p>
                      ))}

                      {rowRequiredGrants.length > 0 && (
                        <div className="space-y-2">
                          <p className="text-xs text-neutral-400">该脚本需要以下出口授权：</p>
                          {rowRequiredGrants.map((key) => (
                            <label key={key} className="flex items-start gap-2 cursor-pointer">
                              <input
                                type="checkbox"
                                checked={key === 'allowed_ports'
                                  ? Boolean(row.grants.allowed_ports?.length)
                                  : Boolean(row.grants[key as keyof Omit<GrantState, 'allowed_ports'>])}
                                onChange={() => void toggleBatchGrant(row, key)}
                                disabled={rowBusy || row.status === 'installed'}
                                className="mt-0.5 w-4 h-4 rounded border-neutral-700 bg-neutral-900 text-accent-500 disabled:opacity-50"
                              />
                              <span className="text-xs">
                                <span className="font-medium">{GRANT_LABELS[key]?.title ?? key}</span>
                                <span className="block text-neutral-500 mt-0.5">{GRANT_LABELS[key]?.detail ?? ''}</span>
                              </span>
                            </label>
                          ))}
                          {row.preview.missing_grants.length > 0 && (
                            <p className="text-warning text-xs">还缺：{row.preview.missing_grants.join('、')}</p>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </section>
        )}

        {error && (
          <p role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm mb-4">
            {error}
          </p>
        )}

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={importBusy}
            className="flex-1 px-4 py-3 rounded-lg bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void handleBatchInstall()}
            disabled={importBusy || batchReadyCount === 0}
            className="flex-1 px-4 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center justify-center gap-2"
          >
            {batchBusy ? <SpinnerGap size={20} className="animate-spin" /> : <Check size={20} weight="bold" />}
            {batchBusy ? '处理中…' : '安装可用音源'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function SourcesPage() {
  const [sources, setSources] = useState<SourceItem[]>([])
  const [channels, setChannels] = useState<Record<string, SourceHealthRow>>({})
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<Notice>(null)
  const [showImport, setShowImport] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [form, setForm] = useState({ name: '', priority: '0', timeout: '10' })

  const load = useCallback(async () => {
    try {
      const report = await listSources()
      setSources(report.items)
      setNotice(null)
      // The verdict is an addition to this page, not a precondition for it: a
      // deployment that cannot answer the roll-up still gets its source list.
      try {
        const health = await listSourceHealth()
        setChannels(Object.fromEntries(health.sources.map((row) => [row.id, row])))
      } catch {
        setChannels({})
      }
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const replace = (item: SourceItem) =>
    setSources((current) => current.map((entry) => (entry.id === item.id ? item : entry)))

  const toggleEnabled = async (source: SourceItem) => {
    try {
      replace(await updateSource(source.id, { enabled: !source.enabled }))
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    }
  }

  const startEditing = (source: SourceItem) => {
    setEditing(source.id)
    setForm({
      name: source.name ?? '',
      priority: String(source.priority),
      timeout: String(source.timeout),
    })
  }

  const saveEditing = async (source: SourceItem) => {
    const priority = Number.parseInt(form.priority, 10)
    const timeout = Number(form.timeout)
    if (!Number.isInteger(priority) || priority < -1000 || priority > 1000) {
      setNotice({ tone: 'error', text: '优先级必须是 -1000 到 1000 之间的整数' })
      return
    }
    if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 300) {
      setNotice({ tone: 'error', text: '超时必须是 0.1 到 300 秒之间的数字' })
      return
    }
    try {
      const saved = await updateSource(source.id, { name: form.name.trim() || null, priority, timeout })
      replace(saved)
      setEditing(null)
      setNotice({ tone: 'ok', text: `已更新 ${source.name || source.id}${reloadNote(saved.reload)}` })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    }
  }

  const remove = async (source: SourceItem) => {
    if (!window.confirm(`确定删除音源「${source.name || source.id}」吗？安装的脚本也会一并移除。`)) return
    try {
      const removed = await deleteSource(source.id)
      setSources((current) => current.filter((entry) => entry.id !== source.id))
      setNotice({
        tone: 'ok',
        text: `已删除 ${source.id}${removed.uninstalled?.length ? `，并卸载 ${removed.uninstalled.length} 个脚本版本` : ''}${reloadNote(removed.reload)}`,
      })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">音源管理</h1>
          <p className="text-neutral-400 text-sm mt-1">导入、启用与配置 lx 音乐音源脚本</p>
        </div>
        <button
          type="button"
          onClick={() => setShowImport(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 font-medium transition-colors"
        >
          <Plus size={20} weight="bold" />
          导入音源
        </button>
      </div>

      {notice && (
        <p
          role="status"
          className={`px-4 py-3 rounded-lg border text-sm mb-4 ${
            notice.tone === 'ok'
              ? 'bg-success/10 border-success/20 text-success'
              : 'bg-danger/10 border-danger/20 text-danger'
          }`}
        >
          {notice.text}
        </p>
      )}

      <div className="grid gap-4">
        {sources.map((source) => (
          <div key={source.id} className="glass rounded-xl p-6">
            {editing === source.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  void saveEditing(source)
                }}
                className="flex flex-wrap items-end gap-3"
              >
                <div className="flex-1 min-w-[12rem]">
                  <label htmlFor={`name-${source.id}`} className="block text-xs text-neutral-400 mb-1">
                    显示名称
                  </label>
                  <input
                    id={`name-${source.id}`}
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder={source.id}
                    className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm"
                  />
                </div>
                <div className="w-28">
                  <label htmlFor={`priority-${source.id}`} className="block text-xs text-neutral-400 mb-1">
                    优先级
                  </label>
                  <input
                    id={`priority-${source.id}`}
                    type="number"
                    value={form.priority}
                    onChange={(e) => setForm({ ...form, priority: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm tabular-nums"
                  />
                </div>
                <div className="w-28">
                  <label htmlFor={`timeout-${source.id}`} className="block text-xs text-neutral-400 mb-1">
                    超时（秒）
                  </label>
                  <input
                    id={`timeout-${source.id}`}
                    type="number"
                    step="0.1"
                    value={form.timeout}
                    onChange={(e) => setForm({ ...form, timeout: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm tabular-nums"
                  />
                </div>
                <button
                  type="submit"
                  className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 font-medium transition-colors flex items-center gap-2"
                >
                  <Check size={18} weight="bold" />
                  保存
                </button>
                <button
                  type="button"
                  onClick={() => setEditing(null)}
                  className="px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 font-medium transition-colors flex items-center gap-2"
                >
                  <X size={18} />
                  取消
                </button>
              </form>
            ) : (
              <div className="flex items-center justify-between gap-4">
                <div className="flex items-center gap-4 min-w-0">
                  <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center flex-shrink-0">
                    <MusicNote size={24} weight="duotone" className="text-accent-400" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="font-semibold truncate">{source.name || source.id}</h3>
                      <span className="text-xs px-2 py-1 rounded-md bg-neutral-800 text-neutral-400 font-mono">
                        {source.id}
                      </span>
                      <span
                        className={`text-xs px-2 py-1 rounded-md ${
                          source.enabled ? 'bg-success/15 text-success' : 'bg-neutral-800 text-neutral-400'
                        }`}
                      >
                        {source.enabled ? '已启用' : '已停用'}
                      </span>
                      {channels[source.id] && (
                        <ChannelBadge row={channels[source.id]} />
                      )}
                      {!source.plugin && (
                        <span className="text-xs px-2 py-1 rounded-md bg-warning/15 text-warning">
                          脚本未安装
                        </span>
                      )}
                    </div>
                    <p className="text-neutral-400 text-sm mt-1">
                      优先级 {source.priority} · 超时 {source.timeout}s
                      {source.plugin ? ` · 版本 ${source.plugin.version} · ${source.plugin.language}` : ''}
                    </p>
                    <p className="text-xs mt-1">
                      <EgressLine source={source} />
                    </p>
                  </div>
                </div>

                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    type="button"
                    onClick={() => void toggleEnabled(source)}
                    title={source.enabled ? '停用该音源' : '启用该音源'}
                    aria-label={source.enabled ? '停用该音源' : '启用该音源'}
                    className={`p-2 rounded-lg transition-colors ${
                      source.enabled
                        ? 'text-success hover:bg-neutral-800'
                        : 'text-neutral-500 hover:bg-neutral-800'
                    }`}
                  >
                    <Power size={20} />
                  </button>
                  <button
                    type="button"
                    onClick={() => startEditing(source)}
                    title="编辑名称、优先级与超时"
                    aria-label="编辑名称、优先级与超时"
                    className="p-2 rounded-lg hover:bg-neutral-800 text-neutral-400 hover:text-neutral-100 transition-colors"
                  >
                    <PencilSimple size={20} />
                  </button>
                  <button
                    type="button"
                    onClick={() => void remove(source)}
                    title="删除该音源"
                    aria-label="删除该音源"
                    className="p-2 rounded-lg hover:bg-danger/10 text-danger transition-colors"
                  >
                    <Trash size={20} />
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}

        {sources.length === 0 && (
          <div className="glass rounded-xl p-12 text-center">
            <FileCode size={48} weight="duotone" className="text-neutral-600 mx-auto mb-4" />
            <h3 className="font-semibold mb-2">暂无音源</h3>
            <p className="text-neutral-400 text-sm mb-4">导入 lx-music 格式的 JS 脚本即可开始搜索与下载</p>
            <button
              type="button"
              onClick={() => setShowImport(true)}
              className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 font-medium transition-colors"
            >
              导入第一个音源
            </button>
          </div>
        )}
      </div>

      {showImport && (
        <ImportDialog
          onClose={() => setShowImport(false)}
          existingIds={new Set(sources.map((source) => source.id))}
          onBatchChanged={() => void load()}
        />
      )}
    </div>
  )
}
