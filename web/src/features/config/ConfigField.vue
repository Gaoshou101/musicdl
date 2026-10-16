<template>
  <div class="grid gap-3 border-t border-neutral-800/65 py-4 md:grid-cols-[minmax(0,1fr)_19rem] md:items-start">
    <div class="min-w-0">
      <div class="flex flex-wrap items-center gap-2">
        <label :for="inputId" class="text-sm font-semibold text-neutral-200">{{ field.label }}</label>
        <span class="pill" :class="sourceClass">{{ sourceText }}</span>
        <span class="pill" :class="field.scope === 'hot' ? 'bg-success/10 text-success' : 'bg-neutral-800 text-neutral-400'">
          {{ field.scope === 'hot' ? '立即生效' : '重启后生效' }}
        </span>
        <span v-if="field.secret" class="inline-flex items-center gap-1 text-[11px] text-neutral-500">
          <LockSimple :size="12" />只写不读
        </span>
      </div>
      <p class="mt-1.5 text-sm leading-relaxed text-neutral-400">{{ field.help }}</p>
      <p class="mt-1 font-mono text-[11px] text-neutral-500">{{ field.env }}</p>
    </div>

    <div class="space-y-2">
      <div v-if="pendingClear" class="flex items-center justify-between gap-3 rounded-xl border border-warning/20 bg-warning/10 px-3 py-2.5 text-sm">
        <span class="text-warning">保存后恢复为部署值</span>
        <button type="button" class="text-xs font-medium text-neutral-300 transition hover:text-neutral-100 disabled:opacity-50" :disabled="disabled" @click="$emit('undoClear', field.key)">撤销</button>
      </div>
      <button
        v-else-if="field.kind === 'bool'"
        :id="inputId"
        type="button"
        role="switch"
        :aria-checked="Boolean(draft)"
        :aria-label="field.label"
        :disabled="disabled"
        class="relative h-7 w-12 rounded-full transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent-400"
        :class="Boolean(draft) ? 'bg-accent-500' : 'bg-neutral-700'"
        @click="$emit('updateDraft', field.key, !Boolean(draft))"
      >
        <span class="absolute top-1 h-5 w-5 rounded-full bg-white shadow transition-all" :class="Boolean(draft) ? 'left-6' : 'left-1'" />
      </button>
      <input
        v-else
        :id="inputId"
        class="field-input"
        :type="field.secret || field.kind === 'secret' ? 'password' : field.kind === 'int' || field.kind === 'float' ? 'number' : 'text'"
        :step="field.kind === 'float' ? 'any' : field.kind === 'int' ? '1' : undefined"
        :value="String(draft ?? '')"
        :placeholder="field.secret || field.kind === 'secret' ? (field.set ? '已设置；留空表示不修改' : '未设置') : field.kind === 'list' ? '用逗号分隔多个值' : ''"
        :autocomplete="field.secret || field.kind === 'secret' ? 'new-password' : 'off'"
        :disabled="disabled"
        @input="$emit('updateDraft', field.key, ($event.target as HTMLInputElement).value)"
      />
      <button
        v-if="!pendingClear && field.source === 'panel'"
        type="button"
        class="inline-flex items-center gap-1.5 text-xs text-neutral-500 transition hover:text-neutral-200"
        :disabled="disabled"
        @click="$emit('clearField', field.key)"
      >
        <ArrowCounterClockwise :size="13" />恢复为部署值
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { PhArrowCounterClockwise as ArrowCounterClockwise, PhLockSimple as LockSimple } from '@phosphor-icons/vue'
import type { ConfigDraft } from '@/features/config/configDraft'
import type { ConfigFieldView } from '@/lib/api'

const props = defineProps<{
  field: ConfigFieldView
  draft: ConfigDraft | undefined
  pendingClear: boolean
  disabled?: boolean
}>()
defineEmits<{
  updateDraft: [key: string, value: ConfigDraft]
  clearField: [key: string]
  undoClear: [key: string]
}>()

const inputId = computed(() => `config-${props.field.key.replace(/[^a-zA-Z0-9_-]/g, '-')}`)
const sourceText = computed(() => ({ panel: '面板覆盖', env: '部署变量', default: '默认值' })[props.field.source])
const sourceClass = computed(() => ({
  panel: 'bg-accent-500/15 text-accent-300',
  env: 'bg-neutral-800 text-neutral-300',
  default: 'bg-neutral-800/60 text-neutral-500',
})[props.field.source])
</script>

<style scoped>
@reference "../../styles/globals.css";
.pill { @apply rounded-full px-2.5 py-1 text-[10px] font-semibold; }
.field-input { @apply w-full rounded-xl border border-neutral-800 bg-neutral-900/55 px-3.5 py-2.5 text-sm transition focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/20; }
</style>
