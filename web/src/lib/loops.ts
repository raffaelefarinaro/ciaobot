/**
 * Chat-owned loops, for display. A loop is an interval automation whose row
 * carries `owner_chat_id`: it runs inside one chat, ends at `expires_at` or
 * after `max_runs` turns, and is deleted when it ends. Pure helpers, no Vue.
 *
 * Every helper tolerates a row from an engine that predates the loop fields:
 * a missing or malformed value renders as "not a loop" or as "ends soon",
 * never as `Invalid Date` or `NaN`.
 */
import type { Schedule } from './types'

type LoopFields = Pick<Schedule, 'owner_chat_id' | 'interval_minutes' | 'expires_at' | 'max_runs' | 'run_count'>

export function isOwnedLoop(s: Partial<LoopFields>): boolean {
  return typeof s.owner_chat_id === 'string' && s.owner_chat_id.trim() !== ''
}

/** The loops that belong to one chat. An automation merely bound to the chat is not one. */
export function loopsForChat(schedules: readonly Schedule[], chatId: string): Schedule[] {
  if (!chatId) return []
  return schedules.filter(s => isOwnedLoop(s) && s.owner_chat_id === chatId)
}

/** Turns the loop may still start. Never negative. */
export function loopRunsLeft(s: Partial<LoopFields>): number {
  const max = Number.isFinite(s.max_runs) ? Number(s.max_runs) : 0
  const ran = Number.isFinite(s.run_count) ? Number(s.run_count) : 0
  return Math.max(0, max - ran)
}

function endInstant(s: Partial<LoopFields>): Date | null {
  if (!s.expires_at) return null
  const at = new Date(s.expires_at)
  return Number.isNaN(at.getTime()) ? null : at
}

function dayKey(d: Date, zone?: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d)
}

/** The end in words: "14:30" when it is today, "Sun 22 Nov, 14:30" otherwise, "soon" when unknown. */
export function loopEndsWhen(s: Partial<LoopFields>, now: number, locale?: string, zone?: string): string {
  const end = endInstant(s)
  if (!end) return 'soon'
  const time = new Intl.DateTimeFormat(locale, { timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(end)
  if (dayKey(end, zone) === dayKey(new Date(now), zone)) return time
  const day = new Intl.DateTimeFormat(locale, { timeZone: zone, weekday: 'short', day: 'numeric', month: 'short' }).format(end)
  return `${day}, ${time}`
}

/** "ends 14:30", "ends Sun 22 Nov, 14:30", or "ends soon". */
export function loopEndsLabel(s: Partial<LoopFields>, now: number, locale?: string, zone?: string): string {
  return `ends ${loopEndsWhen(s, now, locale, zone)}`
}

/** The cadence in words: "every 5 min". */
export function loopCadenceLabel(s: Partial<LoopFields>): string {
  return `every ${s.interval_minutes ?? 0} min`
}

/** Narrow-composer form: "Loop · 5m". */
export function loopShortLabel(s: Partial<LoopFields>): string {
  return `Loop · ${s.interval_minutes ?? 0}m`
}

/** "7 runs left", "1 run left", "0 runs left". */
export function loopRunsLeftLabel(s: Partial<LoopFields>): string {
  const left = loopRunsLeft(s)
  return `${left} ${left === 1 ? 'run' : 'runs'} left`
}

/** Full summary: "every 5 min · ends 14:30 · 7 runs left". */
export function loopSummary(s: Partial<LoopFields>, now: number, locale?: string, zone?: string): string {
  return [loopCadenceLabel(s), loopEndsLabel(s, now, locale, zone), loopRunsLeftLabel(s)].join(' · ')
}
