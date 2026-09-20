'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  ArrowClockwise,
  ArrowCounterClockwise,
  CheckCircle,
  Cube,
  FloppyDisk,
  Info,
  LockSimple,
  Sliders,
  WarningCircle,
} from '@phosphor-icons/react'
import {
  ConfigFieldView,
  ConfigReport,
  errorMessage,
  listConfig,
  reloadNote,
  updateConfig,
} from '@/lib/api'

/** One field's pending edit: the raw text in its input, or a switch's state. */
type Draft = string | boolean
type Drafts = Record<string, Draft>

/**
 * What the server currently shows for a field, as the text its input holds.
 *
 * A secret never arrives with a value, so its draft starts empty and staying
 * empty is what tells the server to keep the stored one.
 */
function shownValue(field: ConfigFieldView): string {
  if (field.value === null || field.value === undefined) return ''
  if (Array.isArray(field.value)) return field.value.join(', ')
  return String(field.value)
}

function initialDrafts(report: ConfigReport): Drafts {
  const drafts: Drafts = {}
  for (const group of report.groups) {
    for (const field of group.fields) {
      if (field.kind === 'bool') drafts[field.key] = Boolean(field.value)
      else drafts[field.key] = field.secret ? '' : shownValue(field)
    }
  }
  return drafts
}

function isDirty(field: ConfigFieldView, drafts: Drafts, cleared: string[]): boolean {
  if (cleared.includes(field.key)) return true
  const draft = drafts[field.key]
  if (draft === undefined) return false
  if (field.kind === 'bool') return draft !== Boolean(field.value)
  return draft !== shownValue(field)
}

/**
 * One field of the pending batch, in the shape the route expects.
 *
 * `null` is the only way to clear an override, which is why it is reserved for
 * the explicit "restore the deployment value" action rather than produced by an
 * empty text box.
 */
function payloadFor(field: ConfigFieldView, drafts: Drafts, cleared: string[]): unknown {
  if (cleared.includes(field.key)) return null
  const draft = drafts[field.key]
  if (field.kind === 'bool') return Boolean(draft)
  if (field.kind === 'int') return Math.trunc(Number(draft))
  if (field.kind === 'float') return Number(draft)
  if (field.kind === 'list') {
    return String(draft)
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean)
  }
  return String(draft)
}

/** A numeric draft the server would reject, caught before the request leaves. */
function numericProblem(field: ConfigFieldView, drafts: Drafts): string | null {
  if (field.kind !== 'int' && field.kind !== 'float') return null
  const raw = String(drafts[field.key] ?? '').trim()
  if (!raw) return `「${field.label}」需要一个数字`
  const parsed = Number(raw)
  if (!Number.isFinite(parsed)) return `「${field.label}」必须是数字`
  if (field.kind === 'int' && !Number.isInteger(parsed)) return `「${field.label}」必须是整数`
  return null
}

const SOURCE_TEXT: Record<ConfigFieldView['source'], string> = {
  panel: '面板覆盖',
  env: '部署变量',
  default: '默认值',
}

const SOURCE_CLASS: Record<ConfigFieldView['source'], string> = {
  panel: 'bg-accent-500/15 text-accent-300',
  env: 'bg-neutral-800 text-neutral-300',
  default: 'bg-neutral-800/60 text-neutral-500',
}

function Badge({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <span className={`px-2 py-0.5 rounded text-xs font-medium ${className}`}>{children}</span>
}

type FieldRowProps = {
  field: ConfigFieldView
  drafts: Drafts
  cleared: string[]
  onDraft: (key: string, value: Draft) => void
  onClear: (key: string) => void
  onUndo: (key: string) => void
}

function FieldRow({ field, drafts, cleared, onDraft, onClear, onUndo }: FieldRowProps) {
  const pending = cleared.includes(field.key)
  const draft = drafts[field.key]
  const inputId = `config-${field.key.replace(/\./g, '-')}`
  const inputClass =
    'w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors text-sm'

  return (
    <div className="grid gap-3 py-4 md:grid-cols-[minmax(0,1fr)_18rem] md:items-start">
      <div>
        <div className="flex items-center gap-2 flex-wrap">
          <label htmlFor={inputId} className="text-sm font-medium">
            {field.label}
          </label>
          <Badge className={SOURCE_CLASS[field.source]}>{SOURCE_TEXT[field.source]}</Badge>
          {field.scope === 'hot' ? (
            <Badge className="bg-success/10 text-success">立即生效</Badge>
          ) : (
            <Badge className="bg-neutral-800/60 text-neutral-400">重启后生效</Badge>
          )}
          {field.secret && (
            <span className="flex items-center gap-1 text-xs text-neutral-500">
              <LockSimple size={12} />
              只写不读
            </span>
          )}
        </div>
        <p className="text-neutral-400 text-sm mt-1">{field.help}</p>
        <p className="text-neutral-600 font-mono text-xs mt-1">{field.env}</p>
      </div>

      <div className="space-y-2">
        {pending ? (
          <div className="flex items-center justify-between gap-3 px-3 py-2 rounded-lg bg-warning/10 border border-warning/20 text-sm">
            <span className="text-warning">保存后恢复为部署值</span>
            <button type="button" onClick={() => onUndo(field.key)} className="text-neutral-300 hover:text-neutral-100">
              撤销
            </button>
          </div>
        ) : field.kind === 'bool' ? (
          <button
            id={inputId}
            type="button"
            role="switch"
            aria-checked={Boolean(draft)}
            aria-label={field.label}
            onClick={() => onDraft(field.key, !Boolean(draft))}
            className={`relative w-12 h-7 rounded-full transition-colors ${
              Boolean(draft) ? 'bg-accent-500' : 'bg-neutral-700'
            }`}
          >
            <span
              className={`absolute top-1 w-5 h-5 rounded-full bg-white transition-all ${
                Boolean(draft) ? 'left-6' : 'left-1'
              }`}
            />
          </button>
        ) : (
          <input
            id={inputId}
            type={field.secret ? 'password' : field.kind === 'int' || field.kind === 'float' ? 'number' : 'text'}
            value={String(draft ?? '')}
            placeholder={field.secret ? (field.set ? '已设置；留空表示不修改' : '未设置') : ''}
            onChange={(event) => onDraft(field.key, event.target.value)}
            step={field.kind === 'float' ? 'any' : undefined}
            className={inputClass}
            autoComplete={field.secret ? 'new-password' : 'off'}
          />
        )}

        {!pending && field.source === 'panel' && (
          <button
            type="button"
            onClick={() => onClear(field.key)}
            className="flex items-center gap-1 text-xs text-neutral-400 hover:text-neutral-200"
          >
            <ArrowCounterClockwise size={12} />
            恢复为部署值
          </button>
        )}
      </div>
    </div>
  )
}

export default function ConfigPage() {
  const [report, setReport] = useState<ConfigReport | null>(null)
  const [drafts, setDrafts] = useState<Drafts>({})
  const [cleared, setCleared] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const next = await listConfig()
      setReport(next)
      setDrafts(initialDrafts(next))
      setCleared([])
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const setDraft = (key: string, value: Draft) => {
    setDrafts((current) => ({ ...current, [key]: value }))
    setCleared((current) => current.filter((item) => item !== key))
  }

  const queueClear = (key: string) => {
    setCleared((current) => (current.includes(key) ? current : [...current, key]))
  }

  const undoClear = (key: string) => {
    setCleared((current) => current.filter((item) => item !== key))
  }

  const discard = () => {
    if (!report) return
    setDrafts(initialDrafts(report))
    setCleared([])
    setMessage(null)
  }

  const save = async () => {
    if (!report) return
    setMessage(null)
    const values: Record<string, unknown> = {}
    for (const group of report.groups) {
      for (const field of group.fields) {
        if (!isDirty(field, drafts, cleared)) continue
        if (!cleared.includes(field.key)) {
          const problem = numericProblem(field, drafts)
          if (problem) {
            setMessage({ type: 'error', text: problem })
            return
          }
        }
        values[field.key] = payloadFor(field, drafts, cleared)
      }
    }
    const count = Object.keys(values).length
    if (!count) {
      setMessage({ type: 'info', text: '没有改动需要保存' })
      return
    }
    setSaving(true)
    try {
      const next = await updateConfig(values)
      setReport(next)
      setDrafts(initialDrafts(next))
      setCleared([])
      setError('')
      setMessage({ type: 'success', text: `已保存 ${count} 项${reloadNote(next.reload) || '，已立即生效'}` })
    } catch (err) {
      setMessage({ type: 'error', text: errorMessage(err) })
    } finally {
      setSaving(false)
    }
  }

  const pendingCount = report
    ? report.groups.reduce(
        (total, group) => total + group.fields.filter((field) => isDirty(field, drafts, cleared)).length,
        0,
      )
    : 0

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="p-8">
      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">运行配置</h1>
          <p className="text-neutral-400 text-sm mt-1">
            这里保存的是面板自己的覆盖层，与后台状态写在同一个文件里；没有覆盖的字段回落到部署时给的值。
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            type="button"
            onClick={() => void load()}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 font-medium transition-colors"
          >
            <ArrowClockwise size={20} />
            刷新
          </button>
          <button
            type="button"
            onClick={() => void save()}
            disabled={saving || pendingCount === 0}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors"
          >
            {saving ? (
              <div className="w-5 h-5 border-2 border-neutral-900 border-t-transparent rounded-full animate-spin" />
            ) : (
              <FloppyDisk size={20} weight="bold" />
            )}
            保存改动{pendingCount ? `（${pendingCount}）` : ''}
          </button>
          {pendingCount > 0 && (
            <button
              type="button"
              onClick={discard}
              className="px-3 py-2 rounded-lg text-sm text-neutral-400 hover:text-neutral-100 transition-colors"
            >
              放弃改动
            </button>
          )}
        </div>
      </div>

      {error && (
        <p role="alert" className="px-4 py-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-sm mb-4">
          {error}
        </p>
      )}

      {message && (
        <p
          role="status"
          className={`px-4 py-3 rounded-lg border text-sm mb-4 ${
            message.type === 'success'
              ? 'bg-success/10 border-success/20 text-success'
              : message.type === 'error'
                ? 'bg-danger/10 border-danger/20 text-danger'
                : 'bg-neutral-800/50 border-neutral-700 text-neutral-300'
          }`}
        >
          {message.text}
        </p>
      )}

      {report && Object.keys(report.rejected).length > 0 && (
        <div className="glass rounded-xl p-5 mb-6 border-warning/30">
          <div className="flex items-center gap-3 mb-3">
            <WarningCircle size={22} weight="fill" className="text-warning" />
            <h2 className="font-semibold">启动时丢弃的旧值</h2>
          </div>
          <p className="text-neutral-400 text-sm mb-3">
            状态文件里有这些键，但当前版本已经不认识或无法接受它们，因此没有生效。保存一次任意改动即可把它们从文件里清掉。
          </p>
          <ul className="space-y-1 text-sm">
            {Object.entries(report.rejected).map(([key, reason]) => (
              <li key={key} className="font-mono text-xs">
                <span className="text-neutral-300">{key}</span>
                <span className="text-neutral-500"> — {reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="glass rounded-xl p-5 mb-6 flex items-start gap-3">
        <Info size={20} weight="duotone" className="text-accent-400 flex-shrink-0 mt-0.5" />
        <p className="text-neutral-400 text-sm leading-relaxed">
          密钥只写不读：面板只会告诉你某一只密钥是否已经设置，留空表示保持原值。改动会先经过一次完整校验，
          任何一项非法都会让整批改动被拒绝，不会出现只保存一半的情况。
        </p>
      </div>

      <div className="space-y-6">
        {report?.groups.map((group) => (
          <div key={group.id} className="glass rounded-xl p-6">
            <div className="flex items-start justify-between gap-4 mb-2">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-lg bg-accent-500/20 flex items-center justify-center flex-shrink-0">
                  <Sliders size={20} weight="duotone" className="text-accent-400" />
                </div>
                <div>
                  <h2 className="font-semibold text-lg">{group.label}</h2>
                  <p className="text-neutral-400 text-sm">{group.help}</p>
                </div>
              </div>
              <span className="text-xs text-neutral-600 font-mono">{group.id}</span>
            </div>
            <div className="divide-y divide-neutral-800/60">
              {group.fields.map((field) => (
                <FieldRow
                  key={field.key}
                  field={field}
                  drafts={drafts}
                  cleared={cleared}
                  onDraft={setDraft}
                  onClear={queueClear}
                  onUndo={undoClear}
                />
              ))}
            </div>
          </div>
        ))}

        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-lg bg-neutral-800 flex items-center justify-center flex-shrink-0">
              <Cube size={20} weight="duotone" className="text-neutral-400" />
            </div>
            <div>
              <h2 className="font-semibold text-lg">容器与部署</h2>
              <p className="text-neutral-400 text-sm">
                这些项由 compose 拥有，面板只如实列出；要改请改宿主机上的 .env 后重建容器。
              </p>
            </div>
          </div>
          <div className="divide-y divide-neutral-800/60">
            {report?.container.map((knob) => (
              <div key={knob.key} className="py-4">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium">{knob.label}</span>
                  {knob.env ? (
                    <Badge className="bg-neutral-800 text-neutral-300 font-mono">{knob.env}</Badge>
                  ) : (
                    <Badge className="bg-neutral-800/60 text-neutral-500">compose 固定</Badge>
                  )}
                </div>
                <p className="text-neutral-400 text-sm mt-1">{knob.help}</p>
              </div>
            ))}
          </div>
        </div>
      </div>

      {!report && !error && (
        <div className="glass rounded-xl p-6 flex items-center gap-3">
          <CheckCircle size={20} weight="fill" className="text-success" />
          <p className="text-sm text-neutral-300">没有可显示的配置。</p>
        </div>
      )}
    </div>
  )
}
