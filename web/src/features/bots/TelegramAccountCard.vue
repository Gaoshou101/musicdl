<template>
  <section class="glass rounded-2xl p-5 sm:p-6" aria-labelledby="telegram-session-title">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div class="flex min-w-0 items-start gap-3 sm:gap-4">
        <div class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-accent-500/15 text-accent-400">
          <TelegramLogo :size="23" weight="duotone" />
        </div>
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <h2 id="telegram-session-title" class="font-semibold">Telegram 账号会话</h2>
            <span class="rounded-full px-2.5 py-1 text-xs font-medium" :class="statusClass">{{ statusText }}</span>
            <span v-if="report && !report.available" class="rounded-full bg-warning/10 px-2.5 py-1 text-xs text-warning">
              连接器未装配
            </span>
          </div>
          <p class="mt-1 text-sm text-neutral-400">
            会话档案 <span class="font-mono text-neutral-300">{{ report?.profile ?? 'default' }}</span>
            <template v-if="report?.pending_phone"> · 等待 <span class="font-mono">{{ report.pending_phone }}</span> 的登录码</template>
          </p>
        </div>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button
          type="button"
          class="action-secondary"
          :disabled="loading || busy"
          :aria-label="loading ? '正在刷新 Telegram 状态' : '刷新 Telegram 状态'"
          @click="refreshStatus"
        >
          <ArrowClockwise :size="17" :class="loading ? 'animate-spin' : ''" />
          刷新状态
        </button>
        <button
          v-if="report?.status === 'ready'"
          type="button"
          class="action-secondary"
          :disabled="busy"
          @click="runAction(() => logoutTelegram(), '已退出登录，下次登录会重新发送验证码')"
        >
          <SignOut :size="17" />
          退出登录
        </button>
      </div>
    </div>

    <div class="mt-4 space-y-3">
      <p v-if="report && !report.enabled" class="flex items-start gap-2 text-sm text-warning">
        <WarningCircle :size="17" class="mt-0.5 shrink-0" />
        运行配置里还没打开 Telegram；打开并保存后立即生效，不用重启。
      </p>
      <p v-if="report?.error" class="flex items-start gap-2 text-sm text-danger" role="alert">
        <WarningCircle :size="17" class="mt-0.5 shrink-0" />
        <span>
          {{ report.error }}
          <template v-if="retrySeconds > 0">，请 {{ retrySeconds }} 秒后重试</template>
        </span>
      </p>
      <p v-if="status === 'invalid_session'" class="text-sm text-neutral-400">
        还差一次首次登录：填写手机号收码。登录成功前，所有 Bot 搜索都会返回错误。
      </p>
      <p v-if="status === 'ready' && (!notice || noticeTone === 'ok')" class="flex items-center gap-2 text-sm text-success">
        <CheckCircle :size="17" weight="fill" />账号已授权，Bot 搜索可以调用。
      </p>

      <form v-if="awaitingPassword" class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end" @submit.prevent="submitPassword">
        <div>
          <label for="telegram-password" class="field-label">两步验证密码</label>
          <input
            id="telegram-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            class="field-input"
            :disabled="busy || loading"
            required
          />
        </div>
        <button class="action-primary sm:min-w-36" type="submit" :disabled="busy || loading || !password || retrySeconds > 0">
          {{ busy ? '提交中…' : retrySeconds > 0 ? `${retrySeconds} 秒后重试` : '提交密码' }}
        </button>
      </form>
      <form v-else-if="waitingForCode" class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end" @submit.prevent="submitCode">
        <div>
          <label for="telegram-code" class="field-label">Telegram 发来的登录码</label>
          <input
            id="telegram-code"
            v-model="code"
            inputmode="numeric"
            autocomplete="one-time-code"
            class="field-input font-mono"
            :disabled="busy || loading"
            required
          />
        </div>
        <button class="action-primary sm:min-w-36" type="submit" :disabled="busy || loading || !code || retrySeconds > 0">
          <CheckCircle :size="17" weight="bold" />
          {{ busy ? '验证中…' : retrySeconds > 0 ? `${retrySeconds} 秒后重试` : '验证并登录' }}
        </button>
      </form>
      <form v-else-if="!report || status === 'invalid_session' || status === 'rate_limited' || status === 'error' || status === null" class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end" @submit.prevent="sendCode">
        <div>
          <label for="telegram-phone" class="field-label">手机号（含国家区号，例如 +8613800138000）</label>
          <input
            id="telegram-phone"
            v-model="phone"
            autocomplete="tel"
            inputmode="tel"
            placeholder="+8613800138000"
            class="field-input font-mono"
            :disabled="busy || loading"
            required
          />
        </div>
        <button class="action-primary sm:min-w-36" type="submit" :disabled="busy || loading || !phone.trim() || retrySeconds > 0">
          <PaperPlaneTilt :size="17" weight="bold" />
          {{ busy ? '发送中…' : retrySeconds > 0 ? `${retrySeconds} 秒后重试` : '发送登录码' }}
        </button>
      </form>

      <div v-if="notice || loadError" class="flex flex-wrap items-center justify-between gap-3 rounded-xl border px-3.5 py-3 text-sm" :class="noticeTone === 'ok' ? 'border-success/20 bg-success/10 text-success' : 'border-danger/20 bg-danger/10 text-danger'" role="status">
        <span>{{ notice || loadError }}</span>
        <button
          v-if="retryAction"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg border border-current/20 px-2.5 py-1.5 text-xs font-medium transition hover:bg-white/5 disabled:opacity-50"
          :disabled="busy || loading || retrySeconds > 0"
          @click="retryLastAction"
        >
          <ArrowClockwise :size="14" />
          {{ retrySeconds > 0 ? `${retrySeconds}s 后重试` : '重试上次操作' }}
        </button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  PhArrowClockwise as ArrowClockwise,
  PhCheckCircle as CheckCircle,
  PhPaperPlaneTilt as PaperPlaneTilt,
  PhSignOut as SignOut,
  PhTelegramLogo as TelegramLogo,
  PhWarningCircle as WarningCircle,
} from '@phosphor-icons/vue'
import {
  beginTelegramLogin,
  errorMessage,
  logoutTelegram,
  readTelegram,
  submitTelegramPassword,
  type TelegramReport,
  verifyTelegramLogin,
} from '@/lib/api'

type RetryAction = () => Promise<TelegramReport>
type NoticeTone = 'ok' | 'error'

const report = ref<TelegramReport | null>(null)
const phone = ref('')
const code = ref('')
const password = ref('')
const busy = ref(false)
const loading = ref(false)
const notice = ref('')
const noticeTone = ref<NoticeTone>('ok')
const loadError = ref('')
const retryAction = ref<RetryAction | null>(null)
const retrySuccessText = ref('重试操作已完成')
const retrySeconds = ref(0)
const passwordChallenge = ref(false)
let retryTimer: ReturnType<typeof setInterval> | undefined
let mounted = true

const status = computed(() => report.value?.status ?? null)
const awaitingPassword = computed(() => status.value === 'password_required' || passwordChallenge.value)
const waitingForCode = computed(() => !awaitingPassword.value && (status.value === 'code_required' || Boolean(report.value?.pending_phone)))
const statusText = computed(() => {
  const labels: Record<string, string> = {
    ready: '已登录',
    code_required: '等待输入登录码',
    password_required: '需要两步验证密码',
    invalid_session: '未登录',
    rate_limited: '被 Telegram 限流',
    error: '会话出错',
  }
  return labels[status.value ?? ''] ?? (loading.value ? '正在读取状态' : '未知状态')
})
const statusClass = computed(() => {
  if (status.value === 'ready') return 'bg-success/10 text-success'
  if (status.value === 'code_required' || status.value === 'password_required') return 'bg-warning/10 text-warning'
  if (status.value === 'rate_limited' || status.value === 'error') return 'bg-danger/10 text-danger'
  return 'bg-neutral-800 text-neutral-400'
})

function applyReport(next: TelegramReport): void {
  report.value = next
  if (next.status === 'password_required') {
    passwordChallenge.value = true
    retryAction.value = null
  }
  else if (next.status === 'ready' || next.status === 'invalid_session' || next.status === 'code_required') passwordChallenge.value = false
  if (typeof next.retry_after === 'number' && next.retry_after > 0) {
    retrySeconds.value = Math.ceil(next.retry_after)
  } else if (next.status !== null) {
    retrySeconds.value = 0
  }
  if (retryTimer) clearInterval(retryTimer)
  if (retrySeconds.value > 0) {
    retryTimer = setInterval(() => {
      retrySeconds.value = Math.max(0, retrySeconds.value - 1)
      if (retrySeconds.value === 0 && retryTimer) {
        clearInterval(retryTimer)
        retryTimer = undefined
      }
    }, 1000)
  }
}

async function runAction(action: RetryAction, successText: string, options: { retryable?: boolean; clearPassword?: boolean } = {}): Promise<void> {
  if (busy.value || loading.value || !mounted) return
  retryAction.value = options.retryable === false ? null : action
  retrySuccessText.value = successText
  busy.value = true
  notice.value = ''
  loadError.value = ''
  try {
    const next = await action()
    if (!mounted) return
    applyReport(next)
    code.value = ''
    password.value = ''
    if (options.clearPassword && next.status !== 'ready') {
      notice.value = next.status === 'rate_limited'
        ? 'Telegram 限流，请等待后重新输入两步验证密码'
        : '两步验证未完成，请重新输入密码后重试'
      noticeTone.value = 'error'
    } else if (next.status === 'rate_limited' || next.status === 'error') {
      notice.value = next.error || (next.status === 'rate_limited' ? 'Telegram 正在限流，请等待后重试' : 'Telegram 会话操作失败')
      noticeTone.value = 'error'
    } else {
      notice.value = successText
      noticeTone.value = 'ok'
    }
  } catch (error) {
    if (!mounted) return
    notice.value = errorMessage(error)
    noticeTone.value = 'error'
  } finally {
    if (options.clearPassword) password.value = ''
    if (mounted) busy.value = false
  }
}

async function refreshStatus(): Promise<void> {
  if (loading.value || busy.value || !mounted) return
  loading.value = true
  notice.value = ''
  loadError.value = ''
  try {
    const next = await readTelegram()
    if (!mounted) return
    applyReport(next)
    retryAction.value = () => readTelegram()
    retrySuccessText.value = 'Telegram 状态已刷新'
  } catch (error) {
    if (!mounted) return
    loadError.value = errorMessage(error)
    noticeTone.value = 'error'
    retryAction.value = () => readTelegram()
    retrySuccessText.value = 'Telegram 状态已刷新'
  } finally {
    if (mounted) loading.value = false
  }
}

function sendCode(): Promise<void> {
  const normalized = phone.value.trim()
  return runAction(() => beginTelegramLogin(normalized), '登录码已发送，请查收')
}

function submitCode(): Promise<void> {
  const submittedCode = code.value.trim()
  return runAction(() => verifyTelegramLogin(submittedCode), '登录码已提交')
}

function submitPassword(): Promise<void> {
  const submittedPassword = password.value
  return runAction(() => submitTelegramPassword(submittedPassword), '两步验证已提交', { retryable: false, clearPassword: true })
}

function retryLastAction(): Promise<void> {
  if (!retryAction.value) return Promise.resolve()
  return runAction(retryAction.value, retrySuccessText.value)
}

onMounted(() => void refreshStatus())
onUnmounted(() => {
  mounted = false
  if (retryTimer) clearInterval(retryTimer)
})
</script>

<style scoped>
@reference "../../styles/globals.css";
.field-label { @apply mb-1.5 block text-xs font-medium text-neutral-400; }
.field-input { @apply w-full rounded-xl border border-neutral-800 bg-neutral-900/55 px-3.5 py-2.5 text-sm transition focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20 disabled:opacity-60; }
.action-primary { @apply inline-flex items-center justify-center gap-2 rounded-xl bg-accent-500 px-4 py-2.5 text-sm font-semibold text-neutral-100 transition hover:bg-accent-600 disabled:cursor-not-allowed disabled:opacity-50; }
.action-secondary { @apply inline-flex items-center justify-center gap-2 rounded-xl border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-xs font-medium text-neutral-300 transition hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50 sm:text-sm; }
</style>
