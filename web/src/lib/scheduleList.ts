/**
 * The sidebar's automation list, shared by the sidebar rows and the open
 * automation's title rail so both walk the same order: One-offs (soonest
 * first), then Routines, then System.
 */
import type { Schedule } from './types'
import { oneOffSortKey, oneOffWhen, scheduleClock } from './scheduleClock'

export interface ScheduleGroups {
  oneOffs: Schedule[]
  routines: Schedule[]
  system: Schedule[]
}

export function groupSidebarSchedules(schedules: readonly Schedule[]): ScheduleGroups {
  return {
    oneOffs: schedules
      .filter(s => s.frequency === 'once')
      .slice()
      .sort((a, b) => oneOffSortKey(a).localeCompare(oneOffSortKey(b))),
    routines: schedules.filter(s => s.frequency !== 'once' && s.scope !== 'system'),
    system: schedules.filter(s => s.frequency !== 'once' && s.scope === 'system'),
  }
}

/** Sidebar order, top to bottom. */
export function sidebarScheduleOrder(schedules: readonly Schedule[]): Schedule[] {
  const g = groupSidebarSchedules(schedules)
  return [...g.oneOffs, ...g.routines, ...g.system]
}

/** A schedule's display name: its title, or the first line of its prompt. */
export function scheduleTitle(s: Pick<Schedule, 'title' | 'prompt'>): string {
  if (s.title) return s.title
  const first = (s.prompt || '').split('\n')[0].trim()
  return first.length > 36 ? first.slice(0, 33) + '...' : first
}

/**
 * The badge in a row's time slot: paused, cadence, or fire time. An interval
 * entry has no time of day, so its cadence goes there instead; a one-off shows
 * its date and time.
 */
export function cadenceBadge(s: Schedule): string {
  if (s.frequency === 'once') return oneOffWhen(s)
  if (!s.enabled) return 'off'
  if (s.frequency === 'interval') return `${s.interval_minutes}m`
  if (s.frequency === 'manual') return '·'
  return scheduleClock(s)
}
