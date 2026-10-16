<template>
  <div class="page-stack overview-page">
    <p v-if="error" role="alert" class="notice notice-error">{{ error }}</p>
    <div v-if="loading" class="panel loading-panel"><span class="spinner" />正在读取服务状态</div>
    <section class="music-hero" aria-labelledby="overview-title">
      <div class="music-hero-copy">
        <p class="eyebrow">音乐工作台</p>
        <h1 id="overview-title">下一首，<br />想听什么？</h1>
        <p class="music-hero-description">搜索歌曲与艺术家，把喜欢的音乐下载到媒体目录。</p>
        <form class="hero-search" @submit.prevent="search">
          <label for="overview-search">从一首歌开始</label>
          <div class="hero-search-controls">
            <input id="overview-search" v-model="query" aria-label="搜索关键词" placeholder="歌曲名称或艺术家" />
            <button class="primary-button" type="submit" :disabled="!query.trim()">搜索音乐 <span aria-hidden="true">↗</span></button>
          </div>
        </form>
      </div>
      <div class="record-artwork" aria-hidden="true">
        <div class="record-sleeve"><span class="sleeve-sun" /></div>
        <div class="record-disc"><span class="record-grooves" /><span class="record-center" /></div>
      </div>
      <span class="music-hero-status health-pill" :class="healthTone"><i />{{ healthLabel }}</span>
    </section>
    <section class="overview-stats" aria-label="服务概览">
      <article class="overview-stat"><p>启用音源</p><strong>{{ sources.enabled }}<small>/ {{ sources.total }}</small></strong><span>可用于搜索与下载</span></article>
      <article class="overview-stat"><p>启用 Bot</p><strong>{{ bots.enabled }}<small>/ {{ bots.total }}</small></strong><span>已注册的 Telegram Bot</span></article>
      <article class="overview-stat"><p>依赖状态</p><strong :class="healthTone">{{ health?.status ?? '未知' }}</strong><span>主服务、Redis 与 Telegram</span></article>
    </section>
    <section v-if="health" class="panel section-panel" aria-labelledby="overview-dependencies">
      <div class="section-heading"><div><h2 id="overview-dependencies">依赖检查</h2><p class="muted">查看支撑搜索与下载流程的服务是否可用。</p></div><span class="subtle">实时读取</span></div>
      <div class="dependency-grid"><div v-for="name in checkNames" :key="name" class="dependency-chip" :class="checkTone(health.checks[name])"><span>{{ checkLabels[name] }}</span><small>{{ checkLabelsByState[health.checks[name]] }}</small></div></div>
    </section>
    <section class="shortcut-grid" aria-label="快捷入口"><RouterLink v-for="link in shortcuts" :key="link.to" :to="link.to" class="panel shortcut-card"><span><strong>{{ link.title }}</strong><small>{{ link.detail }}</small></span><b aria-hidden="true">→</b></RouterLink></section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { errorMessage, listBots, listSources, readHealth, type CheckState, type HealthReport } from '@/lib/api'
import { usePolling } from '@/composables/usePolling'
const router = useRouter()
const sources = ref({ total: 0, enabled: 0 })
const bots = ref({ total: 0, enabled: 0 })
const health = ref<HealthReport | null>(null)
const loading = ref(true)
const error = ref('')
const query = ref('')
const checkNames = ['readyz', 'plugin_runner', 'redis', 'telegram'] as const
const checkLabels = { readyz: '主服务', plugin_runner: '插件执行器', redis: 'Redis', telegram: 'Telegram' }
const checkLabelsByState: Record<CheckState, string> = { ok: '正常', failed: '异常', unavailable: '不可用', not_required: '无需使用' }
const healthTone = computed(() => health.value?.status === 'ok' ? 'tone-success' : health.value ? 'tone-warning' : 'tone-neutral')
const healthLabel = computed(() => health.value?.status === 'ok' ? '服务运行正常' : health.value?.status === 'degraded' ? '需要关注' : '状态未知')
const shortcuts = [{ to: '/dashboard/sources', title: '音源管理', detail: '导入、启用与删除音源脚本' }, { to: '/dashboard/bots', title: 'Bot 管理', detail: '注册可搜索的 Telegram Bot' }, { to: '/dashboard/logs', title: '日志查看', detail: '最近的下载事件与后台操作' }]
function checkTone(state: CheckState) { return state === 'ok' ? 'tone-success' : state === 'failed' || state === 'unavailable' ? 'tone-warning' : 'tone-neutral' }
async function load(signal: AbortSignal) {
  try {
    const [sourceReport, botReport, healthReport] = await Promise.all([listSources(), listBots(), readHealth(signal)])
    if (signal.aborted) return
    sources.value = { total: sourceReport.items.length, enabled: sourceReport.items.filter((item) => item.enabled).length }
    bots.value = { total: botReport.items.length, enabled: botReport.items.filter((item) => item.enabled).length }
    health.value = healthReport; error.value = ''
  } catch (cause) { if (!signal.aborted) error.value = errorMessage(cause) }
  finally { if (!signal.aborted) loading.value = false }
}
usePolling(load, 30_000)
function search() { const value = query.value.trim(); if (value) void router.push({ path: '/dashboard/search', query: { q: value } }) }
</script>
