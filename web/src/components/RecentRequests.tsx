import type { RecentRequest } from '@/lib/api'

const KIND_TEXT: Record<RecentRequest['kind'], string> = {
  search: '搜索',
  download: '下载',
}

const STATUS_TEXT: Record<RecentRequest['status'], string> = {
  success: '成功',
  failure: '失败',
  unknown: '结果未知',
  excluded: '取消或本地原因，不计入成功率',
}

const STATUS_COLOR: Record<RecentRequest['status'], string> = {
  success: 'var(--color-success)',
  failure: 'var(--color-danger)',
  unknown: 'var(--color-neutral-400)',
  excluded: 'var(--color-neutral-400)',
}

function timestampText(value: number): string {
  if (!Number.isFinite(value)) return '时间未知'
  const date = new Date(value * 1000)
  if (!Number.isFinite(date.getTime())) return '时间未知'
  const two = (part: number) => String(part).padStart(2, '0')
  return `${two(date.getMonth() + 1)}/${two(date.getDate())} ${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`
}

function describe(request: RecentRequest): string {
  return `${KIND_TEXT[request.kind]}${STATUS_TEXT[request.status]}`
}

/** Show only observed requests; no evidence is represented by no bars. */
export default function RecentRequests({
  requests,
}: {
  requests: RecentRequest[] | undefined
}) {
  const visibleRequests = (requests ?? []).slice(-10)
  if (requests === undefined) {
    return (
      <div className="glass mt-3 rounded-lg px-3 py-2 text-center">
        <h4 className="text-xs font-medium text-neutral-300">最近请求</h4>
        <p className="mt-1 text-xs text-neutral-500">后端尚未提供近期请求记录</p>
      </div>
    )
  }

  const latest = visibleRequests[visibleRequests.length - 1]
  return (
    <section
      aria-label="最近请求"
      className="glass mt-3 rounded-lg px-3 py-2"
    >
      <h4 className="text-center text-xs font-medium text-neutral-300">最近请求</h4>
      {latest ? (
        <>
          <p className="mt-1 text-center text-xs font-medium tabular-nums text-neutral-300">
            {timestampText(latest.timestamp)}
          </p>
          <div className="mt-2 flex min-h-8 items-end justify-center gap-2">
            {visibleRequests.map((request, index) => {
              const when = timestampText(request.timestamp)
              const label = `${when} · ${describe(request)}`
              return (
                <span
                  key={`${request.kind}-${request.timestamp}-${index}`}
                  role="img"
                  tabIndex={0}
                  title={label}
                  aria-label={label}
                  className="h-8 w-[10px] shrink-0 rounded-[5px] outline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent-500"
                  style={{
                    backgroundColor: request.status === 'excluded' ? 'transparent' : STATUS_COLOR[request.status],
                    border: request.status === 'excluded'
                      ? `2px dashed ${STATUS_COLOR.excluded}`
                      : '1px solid transparent',
                  }}
                />
              )
            })}
          </div>
        </>
      ) : (
        <p className="mt-1 text-center text-xs text-neutral-500">暂无近期请求</p>
      )}
    </section>
  )
}
