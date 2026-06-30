// Small time helpers for the chat message footer. Renders HH:MM for today
// and "Mon D HH:MM" for older messages. Duration formatter caps at minutes;
// turns longer than an hour are vanishingly rare and the rough "Xm Ys" label
// stays useful.

export function formatTime(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const hhmm = `${hh}:${mm}`
  if (d.toDateString() === now.toDateString()) return hhmm
  const md = d.toLocaleString(undefined, { month: 'short', day: 'numeric' })
  return `${md} ${hhmm}`
}

export function formatDuration(ms?: number): string {
  if (ms == null || !isFinite(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  const rs = Math.round(s - m * 60)
  return rs ? `${m}m ${rs}s` : `${m}m`
}
