<template>
  <div class="mt-4 rounded-2xl border border-neutral-800/80 bg-neutral-900/30 p-4 sm:p-5">
    <div class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div class="flex items-start gap-3">
        <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-500/15 text-accent-300">
          <PlugsConnected :size="21" weight="duotone" />
        </div>
        <div>
          <h3 class="font-semibold">AI 连接测试</h3>
          <p class="mt-1 max-w-2xl text-sm leading-relaxed text-neutral-400">
            使用已保存的配置发送一次最小请求，检查 AI 排序端点。测试不会写入设置。
          </p>
        </div>
      </div>
      <button type="button" class="action-secondary shrink-0" :disabled="running" @click="probe">
        <span v-if="running" class="h-4 w-4 animate-spin rounded-full border-2 border-neutral-300 border-t-transparent" />
        <PlugsConnected v-else :size="17" />
        {{ running ? '测试中…' : '测试连接' }}
      </button>
    </div>

    <p v-if="dirty" class="mt-3 text-xs text-warning">有未保存的改动；此测试检查的是已经保存的配置。</p>
    <p v-if="failure" class="mt-3 rounded-xl border border-danger/20 bg-danger/10 px-3.5 py-3 text-sm text-danger" role="alert">{{ failure }}</p>
    <div v-if="report" class="mt-4 rounded-xl border px-3.5 py-3" :class="report.ok ? 'border-success/25 bg-success/10' : 'border-danger/25 bg-danger/10'" role="status">
      <div class="flex flex-wrap items-center gap-2">
        <CheckCircle v-if="report.ok" :size="18" weight="fill" class="text-success" />
        <WarningCircle v-else :size="18" weight="fill" class="text-danger" />
        <strong class="text-sm" :class="report.ok ? 'text-success' : 'text-danger'">{{ report.ok ? '连接成功' : '连接失败' }}</strong>
        <span class="text-xs text-neutral-400">
          {{ report.ok
            ? `${report.model ?? '模型未知'} 用了 ${report.took_ms} ms`
            : `${report.code ? AI_PROBE_TEXT[report.code] : '端点没有给出可读的原因'}${report.code && report.code !== 'disabled' && report.code !== 'unconfigured' ? `（${report.took_ms} ms / 上限 ${report.budget_ms} ms）` : ''}` }}
        </span>
      </div>
      <dl class="mt-3 grid gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
        <div v-if="report.detail" class="flex gap-2"><dt class="shrink-0 text-neutral-500">端点回话</dt><dd class="break-all font-mono text-neutral-300">{{ report.detail }}</dd></div>
        <div v-if="report.code && !report.detail" class="flex gap-2"><dt class="shrink-0 text-neutral-500">失败代码</dt><dd class="font-mono text-neutral-300">{{ report.code }}</dd></div>
        <div class="flex gap-2"><dt class="shrink-0 text-neutral-500">模型</dt><dd class="break-all font-mono text-neutral-300">{{ report.model ?? '未填写' }}</dd></div>
        <div class="flex gap-2"><dt class="shrink-0 text-neutral-500">探针预算</dt><dd class="font-mono text-neutral-300">{{ report.budget_ms ? `${report.budget_ms} ms` : '未发请求' }}</dd></div>
      </dl>
      <p v-if="report.reply" class="mt-2 break-all font-mono text-xs text-neutral-400">{{ report.reply }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import {
  PhCheckCircle as CheckCircle,
  PhPlugsConnected as PlugsConnected,
  PhWarningCircle as WarningCircle,
} from '@phosphor-icons/vue'
import { AI_PROBE_TEXT, errorMessage, testAIEndpoint, type AIProbeReport } from '@/lib/api'

defineProps<{ dirty: boolean }>()
const running = ref(false)
const report = ref<AIProbeReport | null>(null)
const failure = ref('')
let mounted = true

async function probe(): Promise<void> {
  if (running.value || !mounted) return
  running.value = true
  failure.value = ''
  report.value = null
  try {
    const next = await testAIEndpoint()
    if (mounted) report.value = next
  } catch (error) {
    if (mounted) failure.value = errorMessage(error)
  } finally {
    if (mounted) running.value = false
  }
}

onBeforeUnmount(() => { mounted = false })
</script>

<style scoped>
@reference "../../styles/globals.css";
.action-secondary { @apply inline-flex items-center justify-center gap-2 rounded-xl border border-neutral-800 bg-neutral-900/50 px-4 py-2.5 text-sm font-medium text-neutral-200 transition hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50; }
</style>
