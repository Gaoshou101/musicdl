'use client'

import { useCallback, useEffect, useState } from 'react'
import { Robot, Plus, Trash, PencilSimple, Check, X, Power } from '@phosphor-icons/react'
import {
  BotItem,
  createBot,
  deleteBot,
  errorMessage,
  listBots,
  reloadNote,
  updateBot,
} from '@/lib/api'
import TelegramAccountCard from '@/components/TelegramAccountCard'

type Notice = { tone: 'ok' | 'error'; text: string } | null

type Draft = { id: string; username: string; command_template: string; priority: string; timeout: string }

const BLANK_DRAFT: Draft = { id: '', username: '', command_template: '', priority: '0', timeout: '10' }

/** The backend's own rule, restated so the operator is told before saving. */
const USERNAME_HINT = '以字母开头，长度 5-32，只能包含字母、数字和下划线，不带 @'

function parseDraft(draft: Draft): { ok: true; value: { priority: number; timeout: number } } | { ok: false; text: string } {
  const priority = Number.parseInt(draft.priority, 10)
  const timeout = Number(draft.timeout)
  if (!Number.isInteger(priority) || priority < -1000 || priority > 1000) {
    return { ok: false, text: '优先级必须是 -1000 到 1000 之间的整数' }
  }
  if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 120) {
    return { ok: false, text: '超时必须是 0.1 到 120 秒之间的数字' }
  }
  return { ok: true, value: { priority, timeout } }
}

export default function BotsPage() {
  const [bots, setBots] = useState<BotItem[]>([])
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<Notice>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [addDraft, setAddDraft] = useState<Draft>(BLANK_DRAFT)
  const [editing, setEditing] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<Draft>(BLANK_DRAFT)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const report = await listBots()
      setBots(report.items)
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

  const replace = (item: BotItem) =>
    setBots((current) => current.map((entry) => (entry.id === item.id ? item : entry)))

  const handleAdd = async () => {
    const parsed = parseDraft(addDraft)
    if (!parsed.ok) {
      setNotice({ tone: 'error', text: parsed.text })
      return
    }
    setBusy(true)
    try {
      const created = await createBot({
        id: addDraft.id.trim(),
        username: addDraft.username.trim(),
        command_template: addDraft.command_template.trim() || undefined,
        priority: parsed.value.priority,
        timeout: parsed.value.timeout,
      })
      setBots((current) => [...current, created])
      setShowAdd(false)
      setAddDraft(BLANK_DRAFT)
      setNotice({ tone: 'ok', text: `已添加 ${created.id}${reloadNote(created.reload)}` })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    } finally {
      setBusy(false)
    }
  }

  const startEditing = (bot: BotItem) => {
    setEditing(bot.id)
    setEditDraft({
      id: bot.id,
      username: bot.username ?? '',
      command_template: bot.command_template ?? '',
      priority: String(bot.priority),
      timeout: String(bot.timeout),
    })
  }

  const saveEditing = async (bot: BotItem) => {
    const parsed = parseDraft(editDraft)
    if (!parsed.ok) {
      setNotice({ tone: 'error', text: parsed.text })
      return
    }
    setBusy(true)
    try {
      const saved = await updateBot(bot.id, {
        username: editDraft.username.trim(),
        // An empty template is how an operator switches back to the built-in
        // public `/search {query}` contract.
        command_template: editDraft.command_template.trim() || null,
        priority: parsed.value.priority,
        timeout: parsed.value.timeout,
      })
      replace(saved)
      setEditing(null)
      setNotice({ tone: 'ok', text: `已更新 ${bot.id}${reloadNote(saved.reload)}` })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    } finally {
      setBusy(false)
    }
  }

  const toggleEnabled = async (bot: BotItem) => {
    try {
      const saved = await updateBot(bot.id, { enabled: !bot.enabled })
      replace(saved)
      setNotice({ tone: 'ok', text: `${saved.enabled ? '已启用' : '已停用'} ${bot.id}${reloadNote(saved.reload)}` })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err) })
    }
  }

  const remove = async (bot: BotItem) => {
    if (!window.confirm(`确定删除 Bot「${bot.id}」吗？`)) return
    try {
      const removed = await deleteBot(bot.id)
      setBots((current) => current.filter((entry) => entry.id !== bot.id))
      setNotice({ tone: 'ok', text: `已删除 ${bot.id}${reloadNote(removed.reload)}` })
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
          <h1 className="text-3xl font-semibold tracking-tight">Bot 管理</h1>
          <p className="text-neutral-400 text-sm mt-1">
            注册可搜索的 Telegram 音乐 Bot：用户名与命令模板决定它怎么被调用
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 font-medium transition-colors"
        >
          <Plus size={20} weight="bold" />
          添加 Bot
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

      <TelegramAccountCard />

      <div className="grid gap-4">
        {bots.map((bot) => (
          <div key={bot.id} className="glass rounded-xl p-6">
            {editing === bot.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  void saveEditing(bot)
                }}
                className="space-y-3"
              >
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <label htmlFor={`bot-username-${bot.id}`} className="block text-xs text-neutral-400 mb-1">
                      Bot 用户名
                    </label>
                    <input
                      id={`bot-username-${bot.id}`}
                      value={editDraft.username}
                      onChange={(e) => setEditDraft({ ...editDraft, username: e.target.value })}
                      className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none font-mono text-sm"
                    />
                  </div>
                  <div>
                    <label htmlFor={`bot-template-${bot.id}`} className="block text-xs text-neutral-400 mb-1">
                      命令模板（留空使用内置 /search {'{query}'}）
                    </label>
                    <input
                      id={`bot-template-${bot.id}`}
                      value={editDraft.command_template}
                      onChange={(e) => setEditDraft({ ...editDraft, command_template: e.target.value })}
                      placeholder="/search {query}"
                      className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none font-mono text-sm"
                    />
                  </div>
                </div>
                <div className="flex flex-wrap items-end gap-3">
                  <div className="w-28">
                    <label htmlFor={`bot-priority-${bot.id}`} className="block text-xs text-neutral-400 mb-1">
                      优先级
                    </label>
                    <input
                      id={`bot-priority-${bot.id}`}
                      type="number"
                      value={editDraft.priority}
                      onChange={(e) => setEditDraft({ ...editDraft, priority: e.target.value })}
                      className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm tabular-nums"
                    />
                  </div>
                  <div className="w-28">
                    <label htmlFor={`bot-timeout-${bot.id}`} className="block text-xs text-neutral-400 mb-1">
                      超时（秒）
                    </label>
                    <input
                      id={`bot-timeout-${bot.id}`}
                      type="number"
                      step="0.1"
                      value={editDraft.timeout}
                      onChange={(e) => setEditDraft({ ...editDraft, timeout: e.target.value })}
                      className="w-full px-3 py-2 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm tabular-nums"
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={busy}
                    className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 font-medium transition-colors flex items-center gap-2"
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
                </div>
              </form>
            ) : (
              <div className="flex items-center justify-between gap-4">
                <div className="flex items-center gap-4 min-w-0">
                  <div className="w-12 h-12 rounded-lg bg-accent-500/20 flex items-center justify-center flex-shrink-0">
                    <Robot size={24} weight="duotone" className="text-accent-400" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="font-semibold truncate">{bot.username ? `@${bot.username}` : bot.id}</h3>
                      <span className="text-xs px-2 py-1 rounded-md bg-neutral-800 text-neutral-400 font-mono">
                        {bot.id}
                      </span>
                      <span
                        className={`text-xs px-2 py-1 rounded-md ${
                          bot.enabled ? 'bg-success/15 text-success' : 'bg-neutral-800 text-neutral-400'
                        }`}
                      >
                        {bot.enabled ? '已启用' : '已停用'}
                      </span>
                    </div>
                    <p className="text-neutral-400 text-sm mt-1">
                      优先级 {bot.priority} · 超时 {bot.timeout}s · 命令{' '}
                      <span className="font-mono">{bot.command_template || '/search {query}（内置）'}</span>
                    </p>
                  </div>
                </div>

                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    type="button"
                    onClick={() => void toggleEnabled(bot)}
                    title={bot.enabled ? '停用该 Bot' : '启用该 Bot'}
                    aria-label={bot.enabled ? '停用该 Bot' : '启用该 Bot'}
                    className={`p-2 rounded-lg transition-colors ${
                      bot.enabled ? 'text-success hover:bg-neutral-800' : 'text-neutral-500 hover:bg-neutral-800'
                    }`}
                  >
                    <Power size={20} />
                  </button>
                  <button
                    type="button"
                    onClick={() => startEditing(bot)}
                    title="编辑该 Bot"
                    aria-label="编辑该 Bot"
                    className="p-2 rounded-lg hover:bg-neutral-800 text-neutral-400 hover:text-neutral-100 transition-colors"
                  >
                    <PencilSimple size={20} />
                  </button>
                  <button
                    type="button"
                    onClick={() => void remove(bot)}
                    title="删除该 Bot"
                    aria-label="删除该 Bot"
                    className="p-2 rounded-lg hover:bg-danger/10 text-danger transition-colors"
                  >
                    <Trash size={20} />
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}

        {bots.length === 0 && (
          <div className="glass rounded-xl p-12 text-center">
            <Robot size={48} weight="duotone" className="text-neutral-600 mx-auto mb-4" />
            <h3 className="font-semibold mb-2">暂无 Bot</h3>
            <p className="text-neutral-400 text-sm mb-4">
              注册一个可搜索的 Telegram Bot 用户名，即可把它作为音源参与搜索
            </p>
            <button
              type="button"
              onClick={() => setShowAdd(true)}
              className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 font-medium transition-colors"
            >
              添加第一个 Bot
            </button>
          </div>
        )}
      </div>

      {showAdd && (
        <div className="fixed inset-0 bg-neutral-950/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
          <form
            onSubmit={(e) => {
              e.preventDefault()
              void handleAdd()
            }}
            className="glass glass-highlight rounded-2xl p-6 w-full max-w-lg"
          >
            <h2 className="text-xl font-semibold mb-1">添加 Telegram Bot</h2>
            <p className="text-neutral-400 text-sm mb-5">
              这里填写的是 Bot 的公开用户名，不是 BotFather 发放的 Token；Token 由部署侧保管。
            </p>

            <div className="space-y-4 mb-6">
              <div>
                <label htmlFor="new-bot-id" className="block text-sm font-medium mb-2">
                  标识 ID
                </label>
                <input
                  id="new-bot-id"
                  value={addDraft.id}
                  onChange={(e) => setAddDraft({ ...addDraft, id: e.target.value })}
                  placeholder="例如 vmusic"
                  required
                  className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors font-mono text-sm"
                />
              </div>
              <div>
                <label htmlFor="new-bot-username" className="block text-sm font-medium mb-2">
                  Bot 用户名
                </label>
                <input
                  id="new-bot-username"
                  value={addDraft.username}
                  onChange={(e) => setAddDraft({ ...addDraft, username: e.target.value })}
                  placeholder="vmusic_bot"
                  required
                  className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors font-mono text-sm"
                />
                <p className="text-neutral-500 text-xs mt-2">{USERNAME_HINT}</p>
              </div>
              <div>
                <label htmlFor="new-bot-template" className="block text-sm font-medium mb-2">
                  命令模板（可选）
                </label>
                <input
                  id="new-bot-template"
                  value={addDraft.command_template}
                  onChange={(e) => setAddDraft({ ...addDraft, command_template: e.target.value })}
                  placeholder="/search {query}"
                  className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 transition-colors font-mono text-sm"
                />
                <p className="text-neutral-500 text-xs mt-2">
                  留空即使用内置的公开契约 /search {'{query}'}；自定义模板必须且只能包含一个 {'{query}'}。
                </p>
              </div>
              <div className="flex gap-3">
                <div className="flex-1">
                  <label htmlFor="new-bot-priority" className="block text-sm font-medium mb-2">
                    优先级
                  </label>
                  <input
                    id="new-bot-priority"
                    type="number"
                    value={addDraft.priority}
                    onChange={(e) => setAddDraft({ ...addDraft, priority: e.target.value })}
                    className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm tabular-nums"
                  />
                </div>
                <div className="flex-1">
                  <label htmlFor="new-bot-timeout" className="block text-sm font-medium mb-2">
                    超时（秒）
                  </label>
                  <input
                    id="new-bot-timeout"
                    type="number"
                    step="0.1"
                    value={addDraft.timeout}
                    onChange={(e) => setAddDraft({ ...addDraft, timeout: e.target.value })}
                    className="w-full px-4 py-3 rounded-lg bg-neutral-900/50 border border-neutral-800 focus:border-accent-500 focus:outline-none text-sm tabular-nums"
                  />
                </div>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => {
                  setShowAdd(false)
                  setAddDraft(BLANK_DRAFT)
                }}
                className="flex-1 px-4 py-3 rounded-lg bg-neutral-800 hover:bg-neutral-700 font-medium transition-colors"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={busy || !addDraft.id.trim() || !addDraft.username.trim()}
                className="flex-1 px-4 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed font-medium transition-colors flex items-center justify-center gap-2"
              >
                <Check size={20} weight="bold" />
                {busy ? '提交中…' : '确认添加'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
