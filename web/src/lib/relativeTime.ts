/** Format an ISO timestamp as one compact relative-time unit. */
export function formatRelative(iso: string, now: Date = new Date()): string {
  const timestamp = Date.parse(iso)
  if (!Number.isFinite(timestamp)) return 'now'

  const elapsedSeconds = Math.max(0, Math.floor((now.getTime() - timestamp) / 1000))
  if (elapsedSeconds < 60) return 'now'

  const minutes = Math.floor(elapsedSeconds / 60)
  if (minutes < 60) return `${minutes}m`

  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`

  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d`

  const weeks = Math.floor(days / 7)
  if (weeks < 4) return `${weeks}w`

  return `${Math.max(1, Math.floor(days / 30))}mo`
}
