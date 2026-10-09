/**
 * Schedule times for display, in the browser's zone.
 *
 * A schedule stores its time of day as `daily_time_utc` plus its own IANA
 * `timezone_name`. Next-run times are already shown in the browser's zone, so
 * cadence lines and sidebar rows convert the same way: the at-time is turned
 * into the instant it fires on the schedule's next occurrence, and that instant
 * is read back in local time. The weekday is shifted by the same number of days
 * when the conversion crosses midnight. Monthly entries, which have no clean
 * way to shift a day-of-month, show their own time with the zone named instead.
 *
 * The detail page's "At" row is not handled here: it keeps naming the configured
 * zone on purpose.
 */
import type { Schedule } from './types'

const WEEK = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const DAY_MS = 86_400_000

interface Ymd {
  y: number
  m: number
  d: number
}

const formatters = new Map<string, Intl.DateTimeFormat>()

function zoneFormatter(tz: string): Intl.DateTimeFormat {
  let fmt = formatters.get(tz)
  if (!fmt) {
    fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      hourCycle: 'h23',
    })
    formatters.set(tz, fmt)
  }
  return fmt
}

/** Wall-clock fields of `ms` as read in `tz`. */
function zoneWall(ms: number, tz: string): Record<string, number> {
  const fields: Record<string, number> = {}
  for (const part of zoneFormatter(tz).formatToParts(ms)) {
    if (part.type !== 'literal') fields[part.type] = Number(part.value)
  }
  return fields
}

/** Offset of `tz` from UTC at `ms`, in milliseconds (positive east of UTC). */
function zoneOffsetMs(ms: number, tz: string): number {
  const w = zoneWall(ms, tz)
  const asUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute)
  return asUtc - Math.floor(ms / 60_000) * 60_000
}

/** The instant at which the wall clock in `tz` reads y-m-d hh:mm. */
function zonedToInstant(ymd: Ymd, hh: number, mm: number, tz: string): number {
  const wall = Date.UTC(ymd.y, ymd.m - 1, ymd.d, hh, mm)
  const first = wall - zoneOffsetMs(wall, tz)
  return wall - zoneOffsetMs(first, tz)
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function parseAt(s: Schedule): { hh: number; mm: number } | null {
  const m = /^(\d{2}):(\d{2})$/.exec(s.daily_time_utc || '')
  if (!m) return null
  const hh = Number(m[1])
  const mm = Number(m[2])
  if (hh > 23 || mm > 59) return null
  return { hh, mm }
}

function zoneValid(tz: string): boolean {
  try {
    zoneFormatter(tz)
    return true
  } catch {
    return false
  }
}

export interface LocalAt {
  /** Local clock, "HH:MM". */
  time: string
  /** Local calendar date, "YYYY-MM-DD" (used for one-off entries). */
  date: string
  /** Local day minus schedule-zone day for the same occurrence: -1, 0 or 1. */
  dayShift: number
}

/**
 * The schedule's at-time, converted to the browser's zone. Returns null when the
 * schedule has no usable time of day or zone.
 *
 * The reference date is the schedule-zone date of `next_run` (or of `now` when
 * there is no next run), so the offset used is the one in force at that
 * occurrence, DST included. A one-off uses its own `run_at_date`.
 */
export function localAt(s: Schedule, now: number = Date.now()): LocalAt | null {
  const at = parseAt(s)
  if (!at || !s.timezone_name || !zoneValid(s.timezone_name)) return null
  const tz = s.timezone_name

  let ref: Ymd | null = null
  if (s.frequency === 'once' && s.run_at_date) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s.run_at_date)
    if (m) ref = { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) }
  }
  if (!ref) {
    const anchor = s.next_run && !Number.isNaN(Date.parse(s.next_run)) ? Date.parse(s.next_run) : now
    const w = zoneWall(anchor, tz)
    ref = { y: w.year, m: w.month, d: w.day }
  }

  const instant = new Date(zonedToInstant(ref, at.hh, at.mm, tz))
  const localDay = Date.UTC(instant.getFullYear(), instant.getMonth(), instant.getDate())
  const refDay = Date.UTC(ref.y, ref.m - 1, ref.d)
  return {
    time: `${pad(instant.getHours())}:${pad(instant.getMinutes())}`,
    date: `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}`,
    dayShift: Math.round((localDay - refDay) / DAY_MS),
  }
}

/**
 * The clock for a row or cadence line: the local time, or, for a monthly entry
 * that the conversion moves to another day, the schedule's own time with its
 * zone named ("00:30 UTC"). Empty when the schedule has no time of day.
 */
export function scheduleClock(s: Schedule, now: number = Date.now()): string {
  if (!s.daily_time_utc) return ''
  const at = localAt(s, now)
  if (!at) return s.timezone_name ? `${s.daily_time_utc} ${zoneLabel(s)}` : s.daily_time_utc
  if (s.frequency === 'monthly' && at.dayShift !== 0) return `${s.daily_time_utc} ${zoneLabel(s)}`
  return at.time
}

/** The zone as a short label: "UTC", or the IANA name for everything else. */
function zoneLabel(s: Schedule): string {
  return s.timezone_name === 'UTC' ? 'UTC' : s.timezone_name
}

/**
 * Plain cadence for a row's sub-line, in local time: "Every day at 08:00",
 * "Weekly on Sat, Sun at 02:01", "Every 30 min".
 */
export function cadenceSummary(s: Schedule, now: number = Date.now()): string {
  if (s.frequency === 'manual') return 'Only when you run it'
  if (s.frequency === 'interval') return `Every ${s.interval_minutes} min`
  const at = localAt(s, now)
  const clock = scheduleClock(s, now)
  const atText = clock ? ` at ${clock}` : ''
  if (s.frequency === 'once') {
    if (!s.run_at_date) return `Once${atText}`
    return `Once on ${at ? at.date : s.run_at_date}${atText}`
  }
  if (s.frequency === 'monthly') return `Monthly on day ${s.day_of_month}${atText}`
  if (s.frequency === 'weekly') {
    if (!s.days_of_week?.length) return `Weekly${atText}`
    const shift = at ? at.dayShift : 0
    const days = s.days_of_week
      .map(d => {
        const idx = WEEK.indexOf(d)
        if (idx < 0) return { order: WEEK.length, name: d }
        const moved = (idx + shift + WEEK.length) % WEEK.length
        return { order: moved, name: WEEK[moved] }
      })
      .sort((a, b) => a.order - b.order)
      .map(d => capitalise(d.name))
    return `Weekly on ${days.join(', ')}${atText}`
  }
  return `Every day${atText}`
}

/** One-off row time for the sidebar: "06-14 09:30" in local time. */
export function oneOffWhen(s: Schedule, now: number = Date.now()): string {
  const at = localAt(s, now)
  if (!at) return `${s.run_at_date?.slice(5) ?? ''} ${s.daily_time_utc}`.trim()
  return `${at.date.slice(5)} ${scheduleClock(s, now)}`
}

/** Sort key for one-offs: full local date and time, so years order correctly. */
export function oneOffSortKey(s: Schedule, now: number = Date.now()): string {
  const at = localAt(s, now)
  if (!at) return `${s.run_at_date || ''} ${s.daily_time_utc || ''}`
  return `${at.date} ${at.time}`
}

function capitalise(day: string): string {
  return day.charAt(0).toUpperCase() + day.slice(1)
}
