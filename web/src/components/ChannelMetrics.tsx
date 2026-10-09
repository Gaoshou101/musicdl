import type { RecentRequest, SourceHealthRow } from '@/lib/api'
import RecentRequests from '@/components/RecentRequests'

function rate(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`
}

/** Keep independent denominators visible, including when no evidence exists. */
export default function ChannelMetrics({ row }: { row: SourceHealthRow | undefined }) {
  if (!row) return null
  const recentRequests = row?.recent_requests
  const metrics = row.metrics
  if (!metrics) {
    return (
      <>
        <p className="text-xs text-neutral-500">后端尚未提供分项统计</p>
        <RecentRequests requests={recentRequests} />
      </>
    )
  }
  return (
    <div className="text-xs text-neutral-400 space-y-1 mt-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <span title="正常答复（含空结果）占已完成搜索的比例">搜索可用率 {rate(metrics.search.rate)} · {metrics.search.samples} 次</span>
        <span title="按请求与来源候选统计下载终态；同一次尝试只计一次，不代表整项任务成功率">
          来源尝试成功率 {rate(metrics.download.rate)} · {metrics.download.samples} 次
        </span>
        <span title="仅统计请求无损且成功下载后可判定的音质；未知不计入分母">
          无损请求兑现率 {rate(metrics.quality.rate)} · 可判定 {metrics.quality.samples} 次
        </span>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-neutral-500">
        <span>本次运行 · 各取最近 {metrics.window_size} 次</span>
        {metrics.download.excluded > 0 && <span>取消或本地原因 {metrics.download.excluded} 次（不计成功率）</span>}
        {metrics.quality.downgraded > 0 && <span className="text-warning">降级 {metrics.quality.downgraded} 次</span>}
        {metrics.quality.unknown > 0 && <span>音质未知 {metrics.quality.unknown} 次</span>}
        <span>{metrics.ranking.eligible
          ? '下载样本已用于选源排序'
          : `下载样本不足 ${metrics.ranking.minimum_samples} 次，统计不参与选源排序`}</span>
      </div>
      <RecentRequests requests={recentRequests} />
    </div>
  )
}
