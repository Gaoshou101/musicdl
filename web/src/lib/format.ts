/** Small display helpers shared by the pages that render backend values. */

/** One duration in seconds as `m:ss`, or a dash when the source omitted it. */
export function formatDuration(seconds?: number | null): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '--:--'
  const whole = Math.round(seconds)
  const minutes = Math.floor(whole / 60)
  const rest = whole % 60
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

/** One byte count in the unit an operator reads it in. */
export function formatBytes(bytes?: number | null): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '未知'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`
}

/** A bitrate the source reported, in kbps. */
export function formatBitrate(bitrate?: number | null): string | null {
  if (typeof bitrate !== 'number' || !Number.isFinite(bitrate) || bitrate <= 0) return null
  return `${bitrate} kbps`
}

/** A short form of a long hash, for a table cell. */
export function shortHash(value?: string | null, length = 12): string {
  if (!value) return '—'
  return value.length <= length ? value : `${value.slice(0, length)}…`
}
