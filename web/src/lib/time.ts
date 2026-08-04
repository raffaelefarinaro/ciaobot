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

// "3 minutes ago" / "2 days ago". Used where the question is how long ago
// something happened, not at what wall-clock time — a status line reading
// "Jul 26 12:38" hides that the run is months stale.
export function formatRelative(iso?: string, now: Date = new Date()): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const seconds = Math.round((now.getTime() - d.getTime()) / 1000)
  if (seconds < 0) return 'just now'
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['second', 60],
    ['minute', 60],
    ['hour', 24],
    ['day', 30],
    ['month', 12],
    ['year', Infinity],
  ]
  let value = seconds
  for (const [unit, step] of units) {
    if (value < step || step === Infinity) {
      if (unit === 'second' && value < 45) return 'just now'
      const rounded = Math.round(value)
      return `${rounded} ${unit}${rounded === 1 ? '' : 's'} ago`
    }
    value = value / step
  }
  return ''
}

export function formatDuration(ms?: number): string {
  if (ms == null || !isFinite(ms) || ms < 0) return ''
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  const rs = Math.round(s - m * 60)
  return rs ? `${m}m ${rs}s` : `${m}m`
}
