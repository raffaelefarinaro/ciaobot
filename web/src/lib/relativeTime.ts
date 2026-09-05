export interface RelativeTimeOptions {
  /**
   * Spell the result out as prose: "just now", "5m ago". Dense chat rows want
   * the bare unit; file listings and prose read better with the suffix.
   */
  suffix?: boolean
  /**
   * Past this many days, return a short absolute date instead of a relative
   * unit. File listings use this — "Aug 3" beats "3w" once something is old
   * enough that the exact day is what you actually want.
   */
  absoluteAfterDays?: number
  now?: Date
}

/**
 * Format an ISO timestamp as one compact relative-time unit.
 *
 * The second argument accepts a bare Date for the common "just give me the
 * unit, relative to this instant" case.
 */
export function formatRelative(
  iso: string,
  options: RelativeTimeOptions | Date = {},
): string {
  const opts: RelativeTimeOptions = options instanceof Date ? { now: options } : options
  const now = opts.now ?? new Date()
  const suffix = opts.suffix ?? false

  const timestamp = Date.parse(iso)
  if (!Number.isFinite(timestamp)) return suffix ? '' : 'now'

  const elapsedSeconds = Math.max(0, Math.floor((now.getTime() - timestamp) / 1000))
  const unit = (value: number, symbol: string) => (suffix ? `${value}${symbol} ago` : `${value}${symbol}`)

  if (elapsedSeconds < 60) return suffix ? 'just now' : 'now'

  const minutes = Math.floor(elapsedSeconds / 60)
  if (minutes < 60) return unit(minutes, 'm')

  const hours = Math.floor(minutes / 60)
  if (hours < 24) return unit(hours, 'h')

  const days = Math.floor(hours / 24)

  // Checked before the week/month buckets so callers that ask for absolute
  // dates never see "1w" for something they wanted dated.
  if (opts.absoluteAfterDays !== undefined && days >= opts.absoluteAfterDays) {
    return new Date(timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }

  if (days < 7) return unit(days, 'd')

  const weeks = Math.floor(days / 7)
  if (weeks < 4) return unit(weeks, 'w')

  return unit(Math.max(1, Math.floor(days / 30)), 'mo')
}


/**
 * Compact age in days as "today" / "5d" / "7mo" / "2y".
 *
 * The one ladder for "how old is this note". The Memory Map's row/detail age
 * and the retirement queue's verification label had grown independent copies
 * of these thresholds — down to the `years >= 2 ? floor : toFixed(1)` rule —
 * while rendering the same note one tab apart, so a change to either left them
 * disagreeing about it.
 *
 * Distinct from `formatRelative` above, which takes an ISO string and uses
 * week buckets for activity timestamps; this takes a day count.
 */
export function formatAgeDays(days: number): string {
  if (!Number.isFinite(days)) return ''
  if (days < 1) return 'today'
  if (days < 30) return `${days}d`
  if (days < 365) return `${Math.floor(days / 30)}mo`
  const years = days / 365
  return `${years >= 2 ? Math.floor(years) : years.toFixed(1)}y`
}
