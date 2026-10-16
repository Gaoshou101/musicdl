<template>
  <div class="page-stack">
    <header class="page-heading-row"><div><p class="eyebrow">运行状况</p><h1>健康监控</h1><p class="muted">依赖状态每 30 秒自动更新一次。</p></div><button type="button" class="secondary-button" :disabled="refreshing" @click="refreshHealth()">↻ {{ refreshing ? '正在刷新' : '刷新' }}</button></header>
    <p v-if="error" role="alert" class="notice notice-error">{{ error }}</p>
    <div v-if="loading && !health" class="panel loading-panel"><span class="spinner" />正在读取健康状态</div>
    <template v-if="health">
      <div class="panel overall-health" :class="health.status === 'degraded' ? 'tone-warning' : 'tone-success'"><span class="health-emblem">{{ health.status === 'degraded' ? '!' : '✓' }}</span><div><strong>{{ health.status === 'degraded' ? '存在异常的依赖项' : '所有依赖项正常' }}</strong><p class="muted">整体状态 <b>{{ health.status }}</b>：只有「异常」和「不可用」会让整体降级，「无需使用」不算故障。</p></div></div>
      <section class="health-check-grid" aria-label="依赖检查"><article v-for="check in checks" :key="check.name" class="panel health-check"><div class="health-check-heading"><span class="summary-icon">⌁</span><div><h2>{{ check.title }}</h2><p class="muted">{{ check.detail }}</p></div></div><p class="state-line" :class="stateClass[health.checks[check.name]]"><span>●</span>{{ stateText[health.checks[check.name]] }} <small>{{ health.checks[check.name] }}</small></p></article></section>
      <section v-if="channels" class="page-stack channel-section">
        <div><h2>渠道健康度</h2><p class="muted">搜索、来源下载尝试和无损交付分别统计，每类取最近 {{ channels.window }} 次结果。服务重启后统计清零；逐次记录可到「下载历史」查看。</p></div>
        <div v-if="channels.sources.length === 0" class="panel empty-state">还没有任何音源。先到「音源管理」导入一个脚本，再回到这里跑一次搜索。</div>
        <article v-for="channel in channels.sources" :key="channel.id" class="panel channel-card">
          <div class="channel-head"><span class="channel-status" :class="channelClass[channel.status]">{{ channelText[channel.status] }}</span><div class="channel-name"><strong>{{ channel.name || channel.id }}</strong><code>{{ channel.id }}</code></div><span v-if="!channel.enabled" class="tag">已停用</span><span v-if="!channel.configured" class="tag">已移除</span><span class="subtle">{{ channel.last_health == null ? '尚无探活记录' : channel.last_health ? '探活通过' : '探活未通过' }}</span></div>
          <ChannelMetrics :row="channel" />
          <p class="muted small channel-error">{{ channel.last_error ? `最近失败：${stageText[channel.last_error_stage ?? ''] ?? channel.last_error_stage} ${errorText[channel.last_error] ?? channel.last_error}` : '没有失败记录' }}</p>
        </article>
      </section>
      <div v-else-if="channelsError" role="status" class="notice notice-warning">渠道健康度暂时读不到：后端可能还是不支持该接口的版本。上面的依赖检查仍然有效，升级后端后刷新本页即可。</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { errorMessage, listSourceHealth, readHealth, type CheckState, type HealthReport, type SourceHealthReport, type SourceHealthVerdict } from '@/lib/api'
import ChannelMetrics from '@/components/ChannelMetrics.vue'
import { usePolling } from '@/composables/usePolling'
const health = ref<HealthReport | null>(null)
const channels = ref<SourceHealthReport | null>(null)
const channelsError = ref(false)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const checks = [
  { name: 'readyz', title: '主服务', detail: '消息与下载任务的后台工作进程是否还在运行' },
  { name: 'plugin_runner', title: '插件执行器', detail: '在沙箱里真正抓取音频的执行器' },
  { name: 'redis', title: 'Redis', detail: '缓存、去重与任务队列；未启用时无需使用' },
  { name: 'telegram', title: 'Telegram', detail: 'Bot 连接与账号会话是否已授权' },
] as const
const stateText: Record<CheckState, string> = { ok: '正常', failed: '异常', unavailable: '不可用', not_required: '无需使用' }
const stateClass: Record<CheckState, string> = { ok: 'tone-success', failed: 'tone-danger', unavailable: 'tone-warning', not_required: 'tone-neutral' }
const channelText: Record<SourceHealthVerdict, string> = { ok: '正常', degraded: '不稳定', failing: '异常', unknown: '未验证' }
const channelClass: Record<SourceHealthVerdict, string> = { ok: 'tone-success', degraded: 'tone-warning', failing: 'tone-danger', unknown: 'tone-neutral' }
const stageText: Record<string, string> = { search: '搜索', download: '下载', refresh: '刷新', health: '探活' }
const errorText: Record<string, string> = { search_timeout: '搜索超时', search_error: '搜索失败', search_invalid: '返回内容不合法', download_failed: '取不到音频', incomplete_audio: '试听片段或文件不完整', media_timeout: '下载超时', health_failed: '探活失败', refresh_failed: '刷新失败', source_unavailable: '音源不可用' }
async function fetchHealth(signal: AbortSignal) {
  refreshing.value = true
  try {
    const [report, channelReport] = await Promise.all([readHealth(signal), listSourceHealth(signal).catch(() => null)])
    if (signal.aborted) return
    health.value = report; channels.value = channelReport; channelsError.value = channelReport === null; error.value = ''
  } catch (cause) { if (!signal.aborted) error.value = errorMessage(cause) }
  finally { if (!signal.aborted) { loading.value = false; refreshing.value = false } }
}
const refreshHealth = usePolling(fetchHealth, 30_000)
</script>
