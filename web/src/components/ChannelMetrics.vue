<template>
  <template v-if="row">
    <template v-if="!row.metrics">
      <p class="muted small">后端尚未提供分项统计</p>
      <RecentRequests :requests="row.recent_requests" />
    </template>
    <div v-else class="channel-metrics">
      <div class="metric-line">
        <span title="正常答复（含空结果）占已完成搜索的比例">搜索可用率 {{ rate(row.metrics.search.rate) }} · {{ row.metrics.search.samples }} 次</span>
        <span title="按请求与来源候选统计下载终态；同一次尝试只计一次，不代表整项任务成功率">来源尝试成功率 {{ rate(row.metrics.download.rate) }} · {{ row.metrics.download.samples }} 次</span>
        <span title="仅统计请求无损且成功下载后可判定的音质；未知不计入分母">无损请求兑现率 {{ rate(row.metrics.quality.rate) }} · 可判定 {{ row.metrics.quality.samples }} 次</span>
      </div>
      <div class="metric-line muted">
        <span>本次运行 · 各取最近 {{ row.metrics.window_size }} 次</span>
        <span v-if="row.metrics.download.excluded > 0">取消或本地原因 {{ row.metrics.download.excluded }} 次（不计成功率）</span>
        <span v-if="row.metrics.quality.downgraded > 0" class="warning">降级 {{ row.metrics.quality.downgraded }} 次</span>
        <span v-if="row.metrics.quality.unknown > 0">音质未知 {{ row.metrics.quality.unknown }} 次</span>
        <span>{{ row.metrics.ranking.eligible ? '下载样本已用于选源排序' : `下载样本不足 ${row.metrics.ranking.minimum_samples} 次，统计不参与选源排序` }}</span>
      </div>
      <RecentRequests :requests="row.recent_requests" />
    </div>
  </template>
</template>

<script setup lang="ts">
import type { SourceHealthRow } from '@/lib/api'
import RecentRequests from './RecentRequests.vue'
defineProps<{ row: SourceHealthRow | undefined }>()
function rate(value: number | null) { return value === null ? '—' : `${Math.round(value * 100)}%` }
</script>
