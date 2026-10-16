<template>
  <section class="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
    <header class="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p class="mb-2 text-xs font-semibold tracking-wide text-accent-300">Bot 接入</p>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">Bot 管理</h1>
        <p class="mt-1.5 max-w-2xl text-sm leading-relaxed text-neutral-400">
          管理参与音乐搜索的 Telegram Bot，并查看负责调用它们的账号会话。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span class="rounded-full border border-neutral-800 bg-neutral-900/35 px-3 py-2 text-xs text-neutral-400">
          {{ enabledCount }} / {{ bots.length }} 已启用
        </span>
        <button type="button" class="action-primary" :disabled="initialLoadPending" @click="openAdd">
          <Plus :size="18" weight="bold" />添加 Bot
        </button>
      </div>
    </header>

    <p v-if="notice" role="status" class="rounded-xl border px-4 py-3 text-sm" :class="notice.tone === 'ok' ? 'border-success/20 bg-success/10 text-success' : 'border-danger/20 bg-danger/10 text-danger'">
      {{ notice.text }}
    </p>

    <TelegramAccountCard />

    <section aria-labelledby="bots-list-title" class="space-y-3">
      <div class="flex items-center justify-between gap-3 px-1">
        <div>
          <h2 id="bots-list-title" class="text-lg font-semibold">已注册的 Bot</h2>
          <p class="mt-0.5 text-xs text-neutral-500">标识 ID 在管理端保持稳定；用户名、模板、优先级和超时控制调用方式。</p>
        </div>
        <button type="button" class="icon-action" :disabled="loading" aria-label="刷新 Bot 列表" @click="loadBots">
          <ArrowClockwise :size="17" :class="loading ? 'animate-spin' : ''" />刷新
        </button>
      </div>

      <div v-if="loading && !bots.length" class="glass flex min-h-40 items-center justify-center rounded-2xl p-8 text-sm text-neutral-400">
        <span class="mr-3 h-5 w-5 animate-spin rounded-full border-2 border-accent-400 border-t-transparent" />正在读取 Bot…
      </div>
      <div v-else-if="loadError && !bots.length" class="glass rounded-2xl p-6">
        <p role="alert" class="text-sm text-danger">{{ loadError }}</p>
        <button type="button" class="action-secondary mt-4" @click="loadBots">重试</button>
      </div>
      <div v-else-if="!bots.length" class="glass glass-highlight rounded-2xl px-5 py-12 text-center sm:px-10">
        <div class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-500/15 text-accent-300">
          <Robot :size="29" weight="duotone" />
        </div>
        <h3 class="mt-4 font-semibold">还没有注册 Bot</h3>
        <p class="mx-auto mt-1.5 max-w-md text-sm leading-relaxed text-neutral-400">
          添加公开用户名和命令模板后，Bot 就会参与搜索。负责调用 Bot 的 Telegram 账号会话可在上方查看。
        </p>
        <button type="button" class="action-primary mt-5" @click="openAdd"><Plus :size="17" />添加第一个 Bot</button>
      </div>
      <article v-for="bot in bots" v-else :key="bot.id" class="glass rounded-2xl p-4 sm:p-5">
        <form v-if="editingId === bot.id" class="space-y-4" @submit.prevent="saveEdit(bot)">
          <div class="flex items-start justify-between gap-4">
            <div>
              <p class="text-xs font-medium uppercase tracking-wide text-accent-300">编辑 Bot</p>
              <h3 class="mt-1 font-mono text-sm text-neutral-300">{{ bot.id }}</h3>
            </div>
            <button type="button" class="icon-action" @click="cancelEdit">取消</button>
          </div>
          <div class="grid gap-4 md:grid-cols-2">
            <label class="field-group">
              <span>Bot 用户名</span>
              <input v-model="editDraft.username" class="field-input font-mono" required autocomplete="off" />
              <small>以字母开头，长度 5–32，只能包含字母、数字和下划线，不带 @。</small>
            </label>
            <label class="field-group">
              <span>命令模板</span>
              <input v-model="editDraft.command_template" class="field-input font-mono" placeholder="/search {query}" autocomplete="off" />
              <small>留空恢复内置命令。自定义模板必须且只能包含一个 <code>{query}</code>。</small>
            </label>
          </div>
          <div class="grid gap-4 sm:grid-cols-[10rem_10rem_1fr] sm:items-end">
            <label class="field-group">
              <span>优先级</span>
              <input :value="editDraft.priority" class="field-input tabular-nums" type="number" min="-1000" max="1000" step="1" required @input="editDraft.priority = ($event.target as HTMLInputElement).value" />
            </label>
            <label class="field-group">
              <span>超时（秒）</span>
              <input :value="editDraft.timeout" class="field-input tabular-nums" type="number" min="0.1" max="120" step="0.1" required @input="editDraft.timeout = ($event.target as HTMLInputElement).value" />
            </label>
            <div class="flex flex-wrap gap-2 sm:justify-end">
              <button type="button" class="action-secondary" :disabled="saving" @click="cancelEdit">取消</button>
              <button type="submit" class="action-primary" :disabled="saving">
                <Check :size="17" weight="bold" />{{ saving ? '保存中…' : '保存设置' }}
              </button>
            </div>
          </div>
        </form>

        <div v-else class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div class="flex min-w-0 items-start gap-3.5">
            <div class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-accent-500/15 text-accent-300">
              <Robot :size="23" weight="duotone" />
            </div>
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="max-w-full truncate font-semibold">{{ bot.username ? `@${bot.username}` : bot.id }}</h3>
                <span class="rounded-md bg-neutral-800/70 px-2 py-1 font-mono text-[11px] text-neutral-400">{{ bot.id }}</span>
                <span class="rounded-full px-2.5 py-1 text-[11px] font-semibold" :class="bot.enabled ? 'bg-success/10 text-success' : 'bg-neutral-800 text-neutral-400'">
                  {{ bot.enabled ? '已启用' : '已停用' }}
                </span>
              </div>
              <p class="mt-1.5 break-words text-sm text-neutral-400">
                优先级 <span class="tabular-nums text-neutral-200">{{ bot.priority }}</span>
                <span class="mx-1 text-neutral-600">·</span>超时 <span class="tabular-nums text-neutral-200">{{ bot.timeout }}s</span>
                <span class="mx-1 text-neutral-600">·</span>命令 <code class="text-xs text-neutral-300">{{ bot.command_template || '/search {query}（内置）' }}</code>
              </p>
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-2 sm:pl-4">
            <button type="button" class="icon-action" :disabled="busyId === bot.id" :aria-label="bot.enabled ? '停用该 Bot' : '启用该 Bot'" @click="toggleEnabled(bot)">
              <Power :size="17" />{{ bot.enabled ? '停用' : '启用' }}
            </button>
            <button type="button" class="icon-action" :disabled="busyId === bot.id" aria-label="编辑该 Bot" @click="startEdit(bot)"><PencilSimple :size="17" />编辑</button>
            <button type="button" class="icon-action text-danger hover:bg-danger/10" :disabled="busyId === bot.id" aria-label="删除该 Bot" @click="removeBot(bot)"><Trash :size="17" />删除</button>
          </div>
        </div>
      </article>
    </section>

    <div v-if="showAdd" class="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-neutral-950/75 p-4 backdrop-blur-sm" @click.self="closeAdd">
      <section class="glass glass-highlight my-auto w-full max-w-2xl rounded-2xl p-5 sm:p-7" role="dialog" aria-modal="true" aria-labelledby="add-bot-title">
        <div class="flex items-start justify-between gap-4">
          <div>
            <p class="text-xs font-semibold tracking-wide text-accent-300">新建 Bot</p>
            <h2 id="add-bot-title" class="mt-1 text-xl font-semibold">添加 Telegram Bot</h2>
            <p class="mt-1.5 text-sm leading-relaxed text-neutral-400">登记公开 Bot 用户名；Token 由部署侧保管，不会在此输入或显示。</p>
          </div>
          <button type="button" class="icon-action shrink-0" aria-label="关闭" @click="closeAdd"><X :size="17" /></button>
        </div>
        <form class="mt-6 space-y-4" @submit.prevent="createNewBot">
          <div class="grid gap-4 sm:grid-cols-2">
            <label class="field-group">
              <span>标识 ID</span>
              <input v-model="addDraft.id" class="field-input font-mono" placeholder="例如 vmusic" autocomplete="off" required />
            </label>
            <label class="field-group">
              <span>Bot 用户名</span>
              <input v-model="addDraft.username" class="field-input font-mono" placeholder="vmusic_bot" autocomplete="off" required />
              <small>以字母开头，长度 5–32，只能包含字母、数字和下划线，不带 @。</small>
            </label>
          </div>
          <label class="field-group">
            <span>命令模板（可选）</span>
            <input v-model="addDraft.command_template" class="field-input font-mono" placeholder="/search {query}" autocomplete="off" />
            <small>留空使用内置的 <code>/search {query}</code>。自定义模板必须且只能包含一个 <code>{query}</code>。</small>
          </label>
          <div class="grid gap-4 sm:grid-cols-2">
            <label class="field-group">
              <span>优先级</span>
              <input :value="addDraft.priority" class="field-input tabular-nums" type="number" min="-1000" max="1000" step="1" required @input="addDraft.priority = ($event.target as HTMLInputElement).value" />
            </label>
            <label class="field-group">
              <span>超时（秒）</span>
              <input :value="addDraft.timeout" class="field-input tabular-nums" type="number" min="0.1" max="120" step="0.1" required @input="addDraft.timeout = ($event.target as HTMLInputElement).value" />
            </label>
          </div>
          <div class="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
            <button type="button" class="action-secondary sm:min-w-28" :disabled="saving" @click="closeAdd">取消</button>
            <button type="submit" class="action-primary sm:min-w-40" :disabled="saving || !addDraft.id.trim() || !addDraft.username.trim()">
              <Plus :size="17" weight="bold" />{{ saving ? '正在添加…' : '确认添加' }}
            </button>
          </div>
        </form>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  PhArrowClockwise as ArrowClockwise,
  PhCheck as Check,
  PhPencilSimple as PencilSimple,
  PhPlus as Plus,
  PhPower as Power,
  PhRobot as Robot,
  PhTrash as Trash,
  PhX as X,
} from '@phosphor-icons/vue'
import TelegramAccountCard from '@/features/bots/TelegramAccountCard.vue'
import { createBot, deleteBot, errorMessage, listBots, reloadNote, updateBot, type BotItem } from '@/lib/api'
import {
  BLANK_BOT_DRAFT,
  botTemplateForCreate,
  botTemplateForUpdate,
  parseBotDraft,
  type BotDraft,
} from '@/features/bots/botDraft'

type Notice = { tone: 'ok' | 'error'; text: string } | null

const bots = ref<BotItem[]>([])
const loading = ref(true)
const initialLoadPending = ref(true)
const loadError = ref('')
const notice = ref<Notice>(null)
const showAdd = ref(false)
const addDraft = ref<BotDraft>({ ...BLANK_BOT_DRAFT })
const editingId = ref<string | null>(null)
const editDraft = ref<BotDraft>({ ...BLANK_BOT_DRAFT })
const saving = ref(false)
const busyId = ref<string | null>(null)
const enabledCount = computed(() => bots.value.filter((bot) => bot.enabled).length)
let mounted = true
let activeList: Promise<{ items: BotItem[] }> | null = null
let listRevision = 0

function invalidateListSnapshots(): void {
  listRevision += 1
}

async function loadBots(): Promise<void> {
  if (activeList) return
  loading.value = true
  const revision = listRevision
  const request = Promise.resolve().then(() => listBots())
  activeList = request
  try {
    const report = await request
    if (!mounted || revision !== listRevision) return
    bots.value = report.items
    loadError.value = ''
    notice.value = null
  } catch (error) {
    if (!mounted || revision !== listRevision) return
    loadError.value = errorMessage(error)
    if (bots.value.length) notice.value = { tone: 'error', text: loadError.value }
  } finally {
    if (activeList === request) activeList = null
    if (mounted) {
      loading.value = false
      initialLoadPending.value = false
    }
  }
}

function openAdd(): void {
  if (initialLoadPending.value) return
  addDraft.value = { ...BLANK_BOT_DRAFT }
  showAdd.value = true
}

function closeAdd(): void {
  if (saving.value) return
  showAdd.value = false
  addDraft.value = { ...BLANK_BOT_DRAFT }
}

async function createNewBot(): Promise<void> {
  if (saving.value || !mounted) return
  const parsed = parseBotDraft(addDraft.value)
  if (!parsed.ok) {
    notice.value = { tone: 'error', text: parsed.text }
    return
  }
  saving.value = true
  notice.value = null
  try {
    const created = await createBot({
      id: addDraft.value.id.trim(),
      username: addDraft.value.username.trim(),
      command_template: botTemplateForCreate(addDraft.value.command_template),
      priority: parsed.value.priority,
      timeout: parsed.value.timeout,
    })
    if (!mounted) return
    invalidateListSnapshots()
    bots.value = [...bots.value, created]
    showAdd.value = false
    addDraft.value = { ...BLANK_BOT_DRAFT }
    notice.value = { tone: 'ok', text: `已添加 ${created.id}${reloadNote(created.reload)}` }
  } catch (error) {
    if (!mounted) return
    notice.value = { tone: 'error', text: errorMessage(error) }
  } finally {
    if (mounted) saving.value = false
  }
}

function startEdit(bot: BotItem): void {
  editingId.value = bot.id
  editDraft.value = {
    id: bot.id,
    username: bot.username ?? '',
    command_template: bot.command_template ?? '',
    priority: String(bot.priority),
    timeout: String(bot.timeout),
  }
}

function cancelEdit(): void {
  editingId.value = null
  editDraft.value = { ...BLANK_BOT_DRAFT }
}

async function saveEdit(bot: BotItem): Promise<void> {
  if (saving.value || !mounted) return
  const parsed = parseBotDraft(editDraft.value)
  if (!parsed.ok) {
    notice.value = { tone: 'error', text: parsed.text }
    return
  }
  saving.value = true
  notice.value = null
  try {
    const saved = await updateBot(bot.id, {
      username: editDraft.value.username.trim(),
      command_template: botTemplateForUpdate(editDraft.value.command_template),
      priority: parsed.value.priority,
      timeout: parsed.value.timeout,
    })
    if (!mounted) return
    invalidateListSnapshots()
    bots.value = bots.value.map((entry) => entry.id === bot.id ? saved : entry)
    editingId.value = null
    notice.value = { tone: 'ok', text: `已更新 ${bot.id}${reloadNote(saved.reload)}` }
  } catch (error) {
    if (!mounted) return
    notice.value = { tone: 'error', text: errorMessage(error) }
  } finally {
    if (mounted) saving.value = false
  }
}

async function toggleEnabled(bot: BotItem): Promise<void> {
  if (busyId.value || !mounted) return
  busyId.value = bot.id
  try {
    const saved = await updateBot(bot.id, { enabled: !bot.enabled })
    if (!mounted) return
    invalidateListSnapshots()
    bots.value = bots.value.map((entry) => entry.id === bot.id ? saved : entry)
    notice.value = { tone: 'ok', text: `${saved.enabled ? '已启用' : '已停用'} ${bot.id}${reloadNote(saved.reload)}` }
  } catch (error) {
    if (!mounted) return
    notice.value = { tone: 'error', text: errorMessage(error) }
  } finally {
    if (mounted) busyId.value = null
  }
}

async function removeBot(bot: BotItem): Promise<void> {
  if (busyId.value || !mounted) return
  if (!window.confirm(`确定删除 Bot「${bot.id}」吗？`)) return
  busyId.value = bot.id
  try {
    const removed = await deleteBot(bot.id)
    if (!mounted) return
    invalidateListSnapshots()
    bots.value = bots.value.filter((entry) => entry.id !== bot.id)
    notice.value = { tone: 'ok', text: `已删除 ${bot.id}${reloadNote(removed.reload)}` }
  } catch (error) {
    if (!mounted) return
    notice.value = { tone: 'error', text: errorMessage(error) }
  } finally {
    if (mounted) busyId.value = null
  }
}

onMounted(() => void loadBots())
onBeforeUnmount(() => { mounted = false })
</script>

<style scoped>
@reference "../styles/globals.css";
.field-group { @apply flex min-w-0 flex-col gap-1.5 text-sm font-medium text-neutral-300; }
.field-group small { @apply text-xs font-normal leading-relaxed text-neutral-500; }
.field-input { @apply w-full rounded-xl border border-neutral-800 bg-neutral-900/55 px-3.5 py-2.5 text-sm text-neutral-100 transition focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20; }
.action-primary { @apply inline-flex items-center justify-center gap-2 rounded-xl bg-accent-500 px-4 py-2.5 text-sm font-semibold text-neutral-100 transition hover:bg-accent-600 disabled:cursor-not-allowed disabled:opacity-50; }
.action-secondary, .icon-action { @apply inline-flex items-center justify-center gap-2 rounded-xl border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-sm font-medium text-neutral-300 transition hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50; }
</style>
