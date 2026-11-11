import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { cadenceBadge, groupSidebarSchedules, scheduleTitle } from '../lib/scheduleList'
import type { RailItem, RailMark } from '../lib/titleRail'
import type { Schedule } from '../lib/types'
import { markRailArrival, takeRailArrival, useTitleRail, type TitleRail } from './useTitleRail'
import { useWorkspaceSchedules } from './useWorkspaceSchedules'

const KEY = 'schedule'

export const takeScheduleRailArrival = (): -1 | 1 | 0 => takeRailArrival(KEY)
export const markScheduleRailArrival = (direction: -1 | 1 | 0): void => markRailArrival(KEY, direction)

/**
 * What the rail says about an automation's timing: its time or cadence, "paused"
 * when it will not run, and nothing for a manual one (its sidebar badge is a bare
 * dot, which means nothing read aloud). A paused one-off is paused too.
 */
export function scheduleRailWhen(s: Schedule): string {
  if (!s.enabled) return 'paused'
  if (s.frequency === 'manual') return ''
  return cadenceBadge(s)
}

/** "<title>, <group>" plus the timing, then ", missed a run" | ", working". */
export function scheduleRailLabel(title: string, group: string, when: string, mark: RailMark | null): string {
  const parts = [title, group]
  if (when) parts.push(when)
  if (mark === 'needs') parts.push('missed a run')
  else if (mark === 'working') parts.push('working')
  return parts.join(', ')
}

/** The open automation's title rail: the workspace's automations in sidebar order. */
export function useScheduleRail(currentId: () => string): TitleRail {
  const store = useProjectStore()
  const router = useRouter()
  const schedules = useWorkspaceSchedules()

  const entries = computed<RailItem[]>(() => {
    const groups = groupSidebarSchedules(schedules.value)
    const item = (s: Schedule, group: string): RailItem => {
      const working = s.enabled && !!s.web_chat_id && store.isChatStreaming(s.web_chat_id)
      const mark: RailMark | null = s.missed ? 'needs' : working ? 'working' : null
      const title = scheduleTitle(s)
      const when = scheduleRailWhen(s)
      return {
        id: s.schedule_id,
        title,
        subtitle: when ? `${group} · ${when}` : group,
        mark,
        titlePending: false,
        label: scheduleRailLabel(title, group, when, mark),
      }
    }
    return [
      ...groups.oneOffs.map(s => item(s, 'One-off')),
      ...groups.routines.map(s => item(s, 'Routine')),
      ...groups.system.map(s => item(s, 'System')),
    ]
  })

  return useTitleRail(KEY, {
    scope: () => store.activeWorkspace,
    currentId,
    entries,
    // A push a guard cancels or redirects resolves with a failure instead of
    // rejecting; rethrow it so the rail drops its travel order and arrival.
    navigate: async id => {
      const failure = await router.push(`/schedules/${id}`)
      if (failure) throw failure
    },
  })
}
