/**
 * The workspace's automations, sorted the way the Automations pages list them,
 * shared by those pages, the sidebar's Next up and the open automation's title
 * rail so all three walk the same order.
 *
 * The Schedules page lists Ciaobot's own system routines first, then the
 * user's routines (one-offs included) by next run with paused ones last; the
 * Loops page lists the chat-owned loops by next run. Pure helpers, no Vue.
 */
import type { Schedule } from './types'
import { scheduleClock, oneOffWhen } from './scheduleClock'
import { isOwnedLoop } from './loops'

export interface AutomationGroups {
  /** Ciaobot's own upkeep (scope 'system'). */
  system: Schedule[]
  /** The user's routines and one-offs. */
  routines: Schedule[]
  /** Chat-owned loops (lib/loops.ts). */
  loops: Schedule[]
}

function nextRunAt(s: Schedule): number {
  const at = Date.parse(s.next_run || '')
  return Number.isFinite(at) ? at : Infinity
}

/**
 * Soonest next run first; an entry with no next run (manual, or a one-off
 * whose time has passed) after the dated ones, and a paused one last. Ties keep
 * a stable, readable order by title.
 */
export function byNextRun(a: Schedule, b: Schedule): number {
  if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
  const diff = nextRunAt(a) - nextRunAt(b)
  if (diff && Number.isFinite(diff)) return diff
  if (diff) return Number.isFinite(nextRunAt(a)) ? -1 : 1
  return scheduleTitle(a).localeCompare(scheduleTitle(b))
}

export function groupAutomations(schedules: readonly Schedule[]): AutomationGroups {
  const loops: Schedule[] = []
  const system: Schedule[] = []
  const routines: Schedule[] = []
  for (const s of schedules) {
    if (isOwnedLoop(s)) loops.push(s)
    else if (s.scope === 'system') system.push(s)
    else routines.push(s)
  }
  return { system: system.sort(byNextRun), routines: routines.sort(byNextRun), loops: loops.sort(byNextRun) }
}

/**
 * Page order, top to bottom: the Schedules page (system routines, then the
 * user's) and then the Loops page. The open automation's title rail walks this.
 */
export function automationOrder(schedules: readonly Schedule[]): Schedule[] {
  const g = groupAutomations(schedules)
  return [...g.system, ...g.routines, ...g.loops]
}

/**
 * The sidebar's Next up: the soonest runs that are coming, routines, one-offs
 * and system routines alike. Paused entries have no run coming; loops are left
 * to their own page, where a five-minute cadence would not crowd everything
 * else out.
 */
export function nextUpSchedules(schedules: readonly Schedule[], limit = 6): Schedule[] {
  return schedules
    .filter(s => s.enabled && !isOwnedLoop(s) && Number.isFinite(nextRunAt(s)))
    .sort((a, b) => nextRunAt(a) - nextRunAt(b))
    .slice(0, limit)
}

/** A schedule's display name: its title, or the first line of its prompt. */
export function scheduleTitle(s: Pick<Schedule, 'title' | 'prompt'>): string {
  if (s.title) return s.title
  const first = (s.prompt || '').split('\n')[0].trim()
  return first.length > 36 ? first.slice(0, 33) + '...' : first
}

/**
 * The badge in a rail segment's time slot: paused, cadence, or fire time. An
 * interval entry has no time of day, so its cadence goes there instead; a
 * one-off shows its date and time.
 */
export function cadenceBadge(s: Schedule): string {
  if (s.frequency === 'once') return oneOffWhen(s)
  if (!s.enabled) return 'off'
  if (s.frequency === 'interval') return `${s.interval_minutes}m`
  if (s.frequency === 'manual') return '·'
  return scheduleClock(s)
}

/** The distance to an instant in one unit: "in 9h", "7h ago", "now"; '' when unknown. */
export function relativeOffset(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return ''
  const at = Date.parse(iso)
  if (!Number.isFinite(at)) return ''
  const diffMs = at - now
  const absMin = Math.round(Math.abs(diffMs) / 60000)
  if (absMin < 1) return 'now'
  let rel: string
  if (absMin < 60) rel = `${absMin}m`
  else if (absMin < 60 * 24) rel = `${Math.round(absMin / 60)}h`
  else rel = `${Math.round(absMin / 1440)}d`
  return diffMs < 0 ? `${rel} ago` : `in ${rel}`
}

/**
 * Short clock and relative form for rows and rails: "Fri 10:00 · in 10h",
 * "Fri 08:00 · 7h ago". Times are in the browser's local zone, the same as
 * Home's next-run line; the automation's own zone is named once, in the "At"
 * row of its detail.
 */
export function relativeWhen(iso: string | null | undefined, now: number = Date.now()): string {
  const offset = relativeOffset(iso, now)
  if (!offset) return '—'
  const clock = new Date(iso!).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
  return `${clock} · ${offset}`
}

/**
 * What a row's end column says about the last run, as a shape and a word:
 * a missed slot or a run that went wrong (the alert triangle), a run in
 * progress (the working grid), a clean last run (the done tick), or nothing
 * yet. A missed slot outranks everything: it is the one the owner must act on.
 */
export type LastRunState = 'missed' | 'failed' | 'working' | 'done' | 'none'

export function lastRunState(s: Schedule, isStreaming: (chatId: string) => boolean): LastRunState {
  if (s.missed) return 'missed'
  if (s.last_status === 'error' || s.last_status === 'skipped' || s.last_status === 'missing-chat') return 'failed'
  if (s.last_status === 'running'
    || (!!s.last_run_chat_id && isStreaming(s.last_run_chat_id))
    || (s.enabled && !!s.web_chat_id && isStreaming(s.web_chat_id))) return 'working'
  // A clean status with no run on record (an imported entry) says nothing yet.
  if (s.last_status === 'ok' && (s.last_dispatched_at || s.last_triggered_on)) return 'done'
  return 'none'
}

/** The word beside the state's glyph ('' where the glyph needs none on screen). */
export function lastRunWord(s: Schedule, state: LastRunState): string {
  if (state === 'missed') return 'missed'
  if (state === 'failed') {
    if (s.last_status === 'missing-chat') return 'chat deleted'
    if (s.last_status === 'skipped') return 'needs you'
    return 'failed'
  }
  return ''
}

/** The state in words for assistive tech and the glyph's tooltip. */
export function lastRunLabel(s: Schedule, state: LastRunState): string {
  if (state === 'missed') return 'Missed a run'
  if (state === 'failed') {
    if (s.last_status === 'missing-chat') return 'Its chat was deleted'
    if (s.last_status === 'skipped') return 'The last run needs you: check the chat'
    return 'The last run failed'
  }
  if (state === 'working') return 'Running now'
  if (state === 'done') return 'Last run finished'
  return ''
}
