<template>
  <section class="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
    <header class="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
      <div>
        <p class="mb-2 text-xs font-semibold tracking-wide text-accent-300">服务运行</p>
        <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">运行配置</h1>
        <p class="mt-1.5 max-w-3xl text-sm leading-relaxed text-neutral-400">
          面板保存自己的配置覆盖；没有覆盖的字段继续使用部署变量或内置默认值。
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button type="button" class="action-secondary" :disabled="loading || saving" @click="loadConfig">
          <ArrowClockwise :size="17" :class="loading ? 'animate-spin' : ''" />刷新
        </button>
        <button v-if="pendingCount" type="button" class="action-quiet" :disabled="loading || saving" @click="discardChanges">放弃改动</button>
        <button type="button" class="action-primary" :disabled="loading || saving || !pendingCount || !report" @click="saveConfig">
          <FloppyDisk :size="17" weight="bold" />
          {{ saving ? '保存中…' : `保存改动${pendingCount ? `（${pendingCount}）` : ''}` }}
        </button>
      </div>
    </header>

    <div v-if="loading && !report" class="glass flex min-h-52 items-center justify-center rounded-2xl p-8 text-sm text-neutral-400">
      <span class="mr-3 h-5 w-5 animate-spin rounded-full border-2 border-accent-400 border-t-transparent" />正在读取运行配置…
    </div>

    <div v-else-if="error && !report" class="glass rounded-2xl p-5 sm:p-6">
      <p role="alert" class="text-sm text-danger">{{ error }}</p>
      <button type="button" class="action-secondary mt-4" @click="loadConfig">重试</button>
    </div>

    <template v-else-if="report">
      <p v-if="error" role="alert" class="rounded-xl border border-danger/20 bg-danger/10 px-4 py-3 text-sm text-danger">{{ error }} · 当前显示最近一次成功读取的配置。</p>
      <p v-if="message" role="status" class="rounded-xl border px-4 py-3 text-sm" :class="message.type === 'success' ? 'border-success/20 bg-success/10 text-success' : message.type === 'error' ? 'border-danger/20 bg-danger/10 text-danger' : 'border-neutral-700 bg-neutral-800/50 text-neutral-300'">
        {{ message.text }}
      </p>

      <section v-if="Object.keys(report.rejected).length" class="glass rounded-2xl border-warning/25 p-5 sm:p-6" aria-labelledby="rejected-config-title">
        <div class="flex items-start gap-3">
          <WarningCircle :size="21" weight="fill" class="mt-0.5 shrink-0 text-warning" />
          <div>
            <h2 id="rejected-config-title" class="font-semibold">启动时丢弃的旧值</h2>
            <p class="mt-1.5 text-sm leading-relaxed text-neutral-400">
              状态文件包含当前版本不认识或无法接受的字段。保存一次有效改动后，这些旧值会从文件中清除。
            </p>
          </div>
        </div>
        <ul class="mt-4 divide-y divide-neutral-800/70 rounded-xl border border-neutral-800/70 bg-neutral-900/25">
          <li v-for="(reason, key) in report.rejected" :key="key" class="grid gap-1 px-3.5 py-2.5 text-xs sm:grid-cols-[minmax(10rem,0.35fr)_1fr]">
            <code class="break-all text-neutral-300">{{ key }}</code>
            <span class="text-neutral-500">{{ reason }}</span>
          </li>
        </ul>
      </section>

      <section class="glass flex items-start gap-3 rounded-2xl p-4 sm:p-5">
        <Info :size="20" weight="duotone" class="mt-0.5 shrink-0 text-accent-300" />
        <div class="space-y-1 text-sm leading-relaxed text-neutral-400">
          <p><strong class="font-semibold text-neutral-200">密钥只写不读。</strong>已有密钥会显示“已设置”；留空表示保持原值。只有显式选择“恢复为部署值”才会移除面板覆盖。</p>
          <p>整批设置会先经过完整校验；其中任何一项不合法，服务器都会拒绝整批改动。</p>
        </div>
      </section>

      <div v-if="report.groups.length" class="space-y-5">
        <section v-for="group in report.groups" :key="group.id" class="glass rounded-2xl p-4 sm:p-6" :aria-labelledby="`config-group-${group.id}`">
          <div class="mb-2 flex flex-wrap items-start justify-between gap-3">
            <div class="flex items-start gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-500/15 text-accent-300">
                <Sliders :size="20" weight="duotone" />
              </div>
              <div>
                <h2 :id="`config-group-${group.id}`" class="font-semibold">{{ group.label }}</h2>
                <p class="mt-1 max-w-3xl text-sm leading-relaxed text-neutral-400">{{ group.help }}</p>
              </div>
            </div>
            <span class="rounded-md border border-neutral-800 px-2 py-1 font-mono text-[10px] text-neutral-500">{{ group.id }}</span>
          </div>
          <ConfigField
            v-for="field in group.fields"
            :key="field.key"
            :field="field"
            :draft="drafts[field.key]"
            :pending-clear="cleared.includes(field.key)"
            :disabled="loading || saving"
            @update-draft="setDraft"
            @clear-field="queueClear"
            @undo-clear="undoClear"
          />
          <AIProbeCard v-if="group.id === 'ai'" :dirty="configGroupIsDirty(report, 'ai', drafts, cleared)" />
        </section>
      </div>
      <section v-else class="glass rounded-2xl p-6 text-sm text-neutral-400">没有可编辑的面板配置。</section>

      <section class="glass rounded-2xl p-4 sm:p-6" aria-labelledby="container-config-title">
        <div class="flex items-start gap-3">
          <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-neutral-800 text-neutral-300"><Cube :size="20" weight="duotone" /></div>
          <div>
            <h2 id="container-config-title" class="font-semibold">容器与部署</h2>
            <p class="mt-1 max-w-3xl text-sm leading-relaxed text-neutral-400">
              这些项由部署环境拥有，面板只显示当前约定；要修改时请调整宿主机上的环境变量并重建容器。
            </p>
          </div>
        </div>
        <div v-if="report.container.length" class="mt-3 divide-y divide-neutral-800/65">
          <article v-for="knob in report.container" :key="knob.key" class="grid gap-1 py-3.5 sm:grid-cols-[minmax(12rem,0.38fr)_1fr] sm:gap-5">
            <div>
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="text-sm font-semibold text-neutral-200">{{ knob.label }}</h3>
                <span v-if="knob.env" class="rounded-md bg-neutral-800/70 px-2 py-1 font-mono text-[10px] text-neutral-400">{{ knob.env }}</span>
                <span v-else class="rounded-md bg-neutral-800/70 px-2 py-1 text-[10px] text-neutral-500">compose 固定</span>
              </div>
              <code class="mt-1 block break-all text-[10px] text-neutral-600">{{ knob.key }}</code>
            </div>
            <p class="text-sm leading-relaxed text-neutral-400">{{ knob.help }}</p>
          </article>
        </div>
        <p v-else class="mt-4 text-sm text-neutral-500">当前没有额外的容器参数。</p>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  PhArrowClockwise as ArrowClockwise,
  PhCube as Cube,
  PhFloppyDisk as FloppyDisk,
  PhInfo as Info,
  PhSliders as Sliders,
  PhWarningCircle as WarningCircle,
} from '@phosphor-icons/vue'
import AIProbeCard from '@/features/config/AIProbeCard.vue'
import ConfigField from '@/features/config/ConfigField.vue'
import {
  collectConfigChanges,
  configGroupIsDirty,
  countConfigChanges,
  createConfigDrafts,
  isConfigFieldDirty,
  type ConfigDraft,
  type ConfigDrafts,
} from '@/features/config/configDraft'
import { errorMessage, listConfig, reloadNote, updateConfig, type ConfigReport } from '@/lib/api'

type Message = { type: 'success' | 'error' | 'info'; text: string } | null

const report = ref<ConfigReport | null>(null)
const drafts = ref<ConfigDrafts>({})
const cleared = ref<string[]>([])
const loading = ref(true)
const saving = ref(false)
const message = ref<Message>(null)
const error = ref('')
const pendingCount = computed(() => report.value ? countConfigChanges(report.value, drafts.value, cleared.value) : 0)
let mounted = true
let activeLoad: Promise<ConfigReport> | null = null

async function loadConfig(): Promise<void> {
  if (saving.value || activeLoad) return
  if (pendingCount.value > 0 && !window.confirm('刷新会丢弃尚未保存的配置改动，仍要刷新吗？')) return
  loading.value = true
  const request = listConfig()
  activeLoad = request
  try {
    const next = await request
    if (!mounted) return
    report.value = next
    drafts.value = createConfigDrafts(next)
    cleared.value = []
    error.value = ''
    message.value = null
  } catch (cause) {
    if (!mounted) return
    error.value = errorMessage(cause)
  } finally {
    if (activeLoad === request) activeLoad = null
    if (mounted) loading.value = false
  }
}

function setDraft(key: string, value: ConfigDraft): void {
  if (loading.value || saving.value) return
  drafts.value = { ...drafts.value, [key]: value }
  cleared.value = cleared.value.filter((item) => item !== key)
}

function queueClear(key: string): void {
  if (loading.value || saving.value) return
  if (!cleared.value.includes(key)) cleared.value = [...cleared.value, key]
}

function undoClear(key: string): void {
  if (loading.value || saving.value) return
  cleared.value = cleared.value.filter((item) => item !== key)
}

function discardChanges(): void {
  if (!report.value || loading.value || saving.value) return
  drafts.value = createConfigDrafts(report.value)
  cleared.value = []
  message.value = null
}

async function saveConfig(): Promise<void> {
  if (!report.value || loading.value || saving.value) return
  message.value = null
  const collected = collectConfigChanges(report.value, drafts.value, cleared.value)
  if (collected.problem) {
    message.value = { type: 'error', text: collected.problem }
    return
  }
  const count = Object.keys(collected.values).length
  if (!count) {
    message.value = { type: 'info', text: '没有改动需要保存。' }
    return
  }

  const restartCount = report.value.groups.flatMap((group) => group.fields)
    .filter((field) => field.scope === 'restart' && isConfigFieldDirty(field, drafts.value, cleared.value)).length
  saving.value = true
  try {
    const next = await updateConfig(collected.values)
    if (!mounted) return
    report.value = next
    drafts.value = createConfigDrafts(next)
    cleared.value = []
    error.value = ''
    const reloadText = reloadNote(next.reload)
    const scopeText = restartCount ? `，其中 ${restartCount} 项在重启后生效` : ''
    message.value = {
      type: next.reload?.status === 'failed' ? 'error' : 'success',
      text: `已保存 ${count} 项${reloadText || '。'}${scopeText}`,
    }
  } catch (cause) {
    if (!mounted) return
    message.value = { type: 'error', text: errorMessage(cause) }
  } finally {
    if (mounted) saving.value = false
  }
}

onMounted(() => void loadConfig())
onBeforeUnmount(() => { mounted = false })
</script>

<style scoped>
@reference "../styles/globals.css";
.action-primary { @apply inline-flex items-center justify-center gap-2 rounded-xl bg-accent-500 px-4 py-2.5 text-sm font-semibold text-neutral-100 transition hover:bg-accent-600 disabled:cursor-not-allowed disabled:opacity-50; }
.action-secondary { @apply inline-flex items-center justify-center gap-2 rounded-xl border border-neutral-800 bg-neutral-900/45 px-3.5 py-2.5 text-sm font-medium text-neutral-300 transition hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50; }
.action-quiet { @apply rounded-lg px-2.5 py-2 text-sm text-neutral-400 transition hover:bg-neutral-800 hover:text-neutral-200 disabled:opacity-50; }
</style>
