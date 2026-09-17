'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  MusicNote,
  Plus,
  Trash,
  PencilSimple,
  Check,
  X,
  UploadSimple,
  FileCode,
  CheckCircle,
  WarningCircle,
  Power,
} from '@phosphor-icons/react'
import {
  ImportPreview,
  SourceImport,
  SourceItem,
  analyzeSource,
  deleteSource,
  errorMessage,
  installSource,
  listSources,
  updateSource,
} from '@/lib/api'
import { formatBytes, shortHash } from '@/lib/format'

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

type GrantState = {
  allow_insecure_http: boolean
  allow_ip_hosts: boolean
  allow_any_host: boolean
  allowed_ports: number[] | null
}

const NO_GRANTS: GrantState = {
  allow_insecure_http: false,
  allow_ip_hosts: false,
  allow_any_host: false,
  allowed_ports: null,
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
  onInstalled,
}: {
  onClose: () => void
  onInstalled: (item: SourceItem) => void
}) {
  const [script, setScript] = useState('')
  const [filename, setFilename] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [language, setLanguage] = useState<'javascript' | 'python'>('javascript')
  const [grants, setGrants] = useState<GrantState>(NO_GRANTS)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const draft = useCallback(
    (): SourceImport => ({
      id: sourceId.trim(),
      script,
      filename: filename || undefined,
      language,
      allow_insecure_http: grants.allow_insecure_http || undefined,
      allow_ip_hosts: grants.allow_ip_hosts || undefined,
      allow_any_host: grants.allow_any_host || undefined,
      allowed_ports: grants.allowed_ports ?? undefined,
    }),
    [sourceId, script, filename, language, grants],
  )

  const runAnalyze = useCallback(async () => {
    if (!script.trim()) return
    setBusy(true)
    setError('')
    try {
      setPreview(await analyzeSource(draft()))
    } catch (err) {
      setPreview(null)
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }, [draft, script])

  const handleFile = async (file: File) => {
    setError('')
    // A new script declares its own needs; nothing carries over from the last.
    setGrants(NO_GRANTS)
    try {
      const text = await file.text()
      setScript(text)
      setFilename(file.name)
      setBusy(true)
      const next = await analyzeSource({
        id: sourceId.trim(),
        script: text,
        filename: file.name,
        language,
      })
      setPreview(next)
      if (!sourceId.trim() && next.id.suggested) setSourceId(next.id.suggested)
    } catch (err) {
      setPreview(null)
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const toggleGrant = async (key: string) => {
    const next: GrantState = { ...grants }
    if (key === 'allowed_ports') {
      next.allowed_ports = grants.allowed_ports ? null : declaredPorts ?? [443]
    } else {
      next[key as 'allow_insecure_http' | 'allow_ip_hosts' | 'allow_any_host'] = !grants[
        key as 'allow_insecure_http' | 'allow_ip_hosts' | 'allow_any_host'
      ]
    }
    setGrants(next)
    if (!script.trim()) return
    setBusy(true)
    setError('')
    try {
      setPreview(
        await analyzeSource({
          id: sourceId.trim(),
          script,
          filename: filename || undefined,
          language,
          allow_insecure_http: next.allow_insecure_http || undefined,
          allow_ip_hosts: next.allow_ip_hosts || undefined,
          allow_any_host: next.allow_any_host || undefined,
          allowed_ports: next.allowed_ports ?? undefined,
        }),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const analysis = (preview?.analysis ?? {}) as AnalysisSummary
  const requiredGrants = preview ? GRANTS.filter((key) => key in (preview.required_grants ?? {})) : []
  const declaredPorts = analysis.allowed_ports?.length ? analysis.allowed_ports : null
  const granted = (key: string): boolean =>
    key === 'allowed_ports' ? Boolean(grants.allowed_ports?.length) : Boolean(grants[key as keyof GrantState])

  const handleInstall = async () => {
    setBusy(true)
    setError('')
    try {
      onInstalled(await installSource(draft()))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-neutral-950/80 backdrop-blur-sm flex items-start justify-center p-4 z-50 overflow-y-auto">
      <div className="glass glass-highlight rounded-2xl p-6 w-full max-w-2xl my-8">
        <h2 className="text-xl font-semibold mb-1">导入音源脚本</h2>
        <p className="text-neutral-400 text-sm mb-5">
          先选择脚本文件，服务会解析它声明的身份与需要的出口授权，确认后再写入。
        </p>

        <label className="block mb-4">
          <div className="border-2 border-dashed border-neutral-800 rounded-xl p-6 text-center hover:border-accent-500/50 transition-colors cursor-pointer">
            <input
              type="file"
              accept=".js,.mjs,.cjs,.py"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) void handleFile(file)
              }}
            />
            <UploadSimple size={40} weight="duotone" className="text-neutral-600 mx-auto mb-3" />
            <p className="font-medium mb-1">{filename || '选择 JS / PY 文件'}</p>
            <p className="text-neutral-400 text-sm">
              {script
                ? `${script.length.toLocaleString('zh-CN')} 个字符，${formatBytes(analysis.size_bytes ?? null)}`
                : '支持 lx-music 音源脚本与普通 JS / Python 脚本'}
            </p>
          </div>
        </label>

        <div className="grid gap-4 sm:grid-cols-2 mb-4">
          <div>
            <label htmlFor="source-id" className="block text-sm font-medium mb-2">
              音源 ID
            </label>
            <input
              id="source-id"
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
              onBlur={() => void runAnalyze()}
              placeholder="例如 lx-xinghai"
              className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors font-mono text-sm"
            />
            <p className="text-neutral-500 text-xs mt-1">
              小写字母、数字与中划线；同一音源的新版本要沿用同一个 ID。
            </p>
          </div>
          <div>
            <label htmlFor="source-language" className="block text-sm font-medium mb-2">
              脚本语言
            </label>
            <select
              id="source-language"
              value={language}
              onChange={(e) => {
                setLanguage(e.target.value as 'javascript' | 'python')
                void runAnalyze()
              }}
              className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors text-sm"
            >
              <option value="javascript">javascript</option>
              <option value="python">python</option>
            </select>
            <p className="text-neutral-500 text-xs mt-1">lx 音源固定为 javascript，无需改动。</p>
          </div>
        </div>

        {preview && (
          <div className="glass rounded-xl p-4 mb-4 space-y-4">
            <div className="flex items-start gap-3">
              {preview.installable ? (
                <CheckCircle size={24} weight="fill" className="text-success flex-shrink-0" />
              ) : (
                <WarningCircle size={24} weight="fill" className="text-warning flex-shrink-0" />
              )}
              <div className="flex-1 min-w-0">
                <h4 className="font-medium">
                  {analysis.name || filename || '未命名音源'}
                  {analysis.version ? <span className="text-neutral-400 font-normal"> v{analysis.version}</span> : null}
                </h4>
                <p className="text-neutral-400 text-sm mt-1">
                  类型 {preview.install_path === 'lx' ? 'lx 自定义音源' : '通用脚本'} · 语言 {preview.language ?? '—'} ·
                  操作 {(preview.operations ?? []).join('/') || '—'}
                </p>
                {analysis.sha256 && (
                  <p className="text-neutral-500 text-xs mt-1 font-mono">sha256 {shortHash(analysis.sha256, 24)}</p>
                )}
              </div>
              <span
                className={`text-xs px-2 py-1 rounded-md whitespace-nowrap ${
                  preview.installable ? 'bg-success/15 text-success' : 'bg-warning/15 text-warning'
                }`}
              >
                {preview.installable ? '可安装' : '需处理'}
              </span>
            </div>

            {!preview.id.valid && (
              <p className="text-warning text-sm">
                ID 不合法：{preview.id.reason || '请改用小写字母、数字与中划线的组合'}
                {preview.id.suggested ? `（建议：${preview.id.suggested}）` : ''}
              </p>
            )}

            {(analysis.blockers ?? []).map((finding) => (
              <p key={finding.code} className="text-danger text-sm">
                拒绝原因 {finding.code}：{finding.detail}
              </p>
            ))}
            {(analysis.caveats ?? []).length > 0 && (
              <ul className="text-neutral-400 text-sm space-y-1">
                {(analysis.caveats ?? []).map((finding) => (
                  <li key={finding.code}>提示 {finding.code}：{finding.detail}</li>
                ))}
              </ul>
            )}

            <dl className="grid gap-2 sm:grid-cols-3 text-sm">
              <div>
                <dt className="text-neutral-400">声明域名</dt>
                <dd className="font-mono text-xs break-all">
                  {(analysis.allowed_hosts ?? []).join('、') || '—'}
                </dd>
              </div>
              <div>
                <dt className="text-neutral-400">声明端口</dt>
                <dd className="font-mono text-xs">{(analysis.allowed_ports ?? [443]).join('/')}</dd>
              </div>
              <div>
                <dt className="text-neutral-400">引用域名</dt>
                <dd className="font-mono text-xs break-all">
                  {(analysis.referenced_hosts ?? []).length} 个
                </dd>
              </div>
            </dl>

            {requiredGrants.length > 0 && (
              <div className="border-t border-neutral-800 pt-4">
                <h5 className="text-sm font-medium mb-3">该脚本需要以下出口授权</h5>
                <div className="space-y-3">
                  {requiredGrants.map((key) => (
                    <label key={key} className="flex items-start gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={granted(key)}
                        onChange={() => void toggleGrant(key)}
                        className="mt-1 w-4 h-4 rounded border-neutral-700 bg-neutral-900 text-accent-500 focus:ring-2 focus:ring-accent-500/20"
                      />
                      <span>
                        <span className="text-sm font-medium">{GRANT_LABELS[key]?.title ?? key}</span>
                        <span className="block text-neutral-400 text-xs mt-0.5">
                          {GRANT_LABELS[key]?.detail ?? ''}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
                {preview.missing_grants.length > 0 && (
                  <p className="text-warning text-sm mt-3">还缺：{preview.missing_grants.join('、')}</p>
                )}
              </div>
            )}

            {preview.refusal && !preview.installable && (
              <p className="text-danger text-sm">服务拒绝安装：{preview.refusal}</p>
            )}
          </div>
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
            className="flex-1 px-4 py-3 rounded-lg bg-neutral-800 hover:bg-neutral-700 font-medium transition-colors"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void handleInstall()}
            disabled={busy || !script.trim() || !preview?.installable}
            className="flex-1 px-4 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center justify-center gap-2"
          >
            <Check size={20} weight="bold" />
            {busy ? '处理中…' : '确认导入'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function SourcesPage() {
  const [sources, setSources] = useState<SourceItem[]>([])
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
      replace(await updateSource(source.id, { name: form.name.trim() || null, priority, timeout }))
      setEditing(null)
      setNotice({ tone: 'ok', text: `已更新 ${source.name || source.id}` })
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
        text: `已删除 ${source.id}${removed.uninstalled?.length ? `，并卸载 ${removed.uninstalled.length} 个脚本版本` : ''}`,
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
          onInstalled={(item) => {
            setShowImport(false)
            setNotice({ tone: 'ok', text: `已导入 ${item.id}` })
            void load()
          }}
        />
      )}
    </div>
  )
}
