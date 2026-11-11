import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { scheduleInWorkspace } from '../lib/automationWorkspace'
import { cadenceBadge, groupSidebarSchedules, scheduleTitle } from '../lib/scheduleList'
import type { RailItem, RailMark } from '../lib/titleRail'
import type { Schedule } from '../lib/types'
import { markRailArrival, takeRailArrival, useTitleRail, type TitleRail } from './useTitleRail'

const KEY = 'schedule'

export const takeScheduleRailArrival = (): -1 | 1 | 0 => takeRailArrival(KEY)
export const markScheduleRailArrival = (direction: -1 | 1 | 0): void => markRailArrival(KEY, direction)

/** "<title>, <group>, <cadence>" plus ", paused" | ", missed a run" | ", working". */
export function scheduleRailLabel(title: string, group: string, when: string, state: 'paused' | RailMark | null): string {
  const parts = [title, group]
  if (when && state !== 'paused') parts.push(when)
  if (state === 'paused') parts.push('paused')
  else if (state === 'needs') parts.push('missed a run')
  else if (state === 'working') parts.push('working')
  return parts.join(', ')
}

/** The open automation's title rail: the workspace's automations in sidebar order. */
export function useScheduleRail(currentId: () => string): TitleRail {
  const store = useProjectStore()
  const taskStore = useTaskStore()
  const router = useRouter()

  const entries = computed<RailItem[]>(() => {
    const groups = groupSidebarSchedules(taskStore.schedules.filter(s => scheduleInWorkspace(
      s,
      store.activeWorkspace,
      store.chats,
      store.projects,
    )))
    const item = (s: Schedule, group: string): RailItem => {
      const working = s.enabled && !!s.web_chat_id && store.isChatStreaming(s.web_chat_id)
      const mark: RailMark | null = s.missed ? 'needs' : working ? 'working' : null
      const title = scheduleTitle(s)
      const when = s.frequency === 'once' || s.enabled ? cadenceBadge(s) : ''
      return {
        id: s.schedule_id,
        title,
        subtitle: s.enabled || s.frequency === 'once' ? `${group} · ${when}` : `${group} · paused`,
        mark,
        titlePending: false,
        label: scheduleRailLabel(title, group, when, !s.enabled ? 'paused' : mark),
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
    navigate: id => router.push(`/schedules/${id}`),
  })
}
