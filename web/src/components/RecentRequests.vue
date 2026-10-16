<template>
  <div v-if="requests === undefined" class="recent-requests">
    <h4>最近请求</h4><p>后端尚未提供近期请求记录</p>
  </div>
  <section v-else class="recent-requests" aria-label="最近请求">
    <h4>最近请求</h4>
    <template v-if="visibleRequests.length">
      <p class="recent-latest">{{ timestampText(visibleRequests[visibleRequests.length - 1]!.timestamp) }}</p>
      <div class="recent-bars">
        <span v-for="(request, index) in visibleRequests" :key="`${request.kind}-${request.timestamp}-${index}`"
          role="img" tabindex="0" class="recent-bar" :aria-label="`${timestampText(request.timestamp)} · ${describe(request)}`"
          :title="`${timestampText(request.timestamp)} · ${describe(request)}`" :style="barStyle(request.status)" />
      </div>
    </template>
    <p v-else>暂无近期请求</p>
  </section>
</template>

<script setup lang="ts">
import type { RecentRequest } from '@/lib/api'
const props = defineProps<{ requests: RecentRequest[] | undefined }>()
const visibleRequests = computed(() => (props.requests ?? []).slice(-10))
const kinds: Record<RecentRequest['kind'], string> = { search: '搜索', download: '下载' }
const statuses: Record<RecentRequest['status'], string> = { success: '成功', failure: '失败', unknown: '结果未知', excluded: '取消或本地原因，不计入成功率' }
const colors: Record<RecentRequest['status'], string> = { success: 'var(--color-success)', failure: 'var(--color-danger)', unknown: 'var(--color-neutral-400)', excluded: 'var(--color-neutral-400)' }
function timestampText(value: number): string {
  if (!Number.isFinite(value)) return '时间未知'
  const date = new Date(value * 1000)
  if (!Number.isFinite(date.getTime())) return '时间未知'
  const two = (part: number) => String(part).padStart(2, '0')
  return `${two(date.getMonth() + 1)}/${two(date.getDate())} ${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`
}
function describe(request: RecentRequest) { return `${kinds[request.kind]}${statuses[request.status]}` }
function barStyle(status: RecentRequest['status']) {
  return status === 'excluded'
    ? { backgroundColor: 'transparent', border: `2px dashed ${colors.excluded}` }
    : { backgroundColor: colors[status], border: '1px solid transparent' }
}
import { computed } from 'vue'
</script>
