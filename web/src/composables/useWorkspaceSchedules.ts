import { computed, type ComputedRef } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { scheduleInWorkspace } from '../lib/automationWorkspace'
import type { Schedule } from '../lib/types'

/** The selected workspace's automations: what the sidebar lists, the overview shows and the title rail walks. */
export function useWorkspaceSchedules(): ComputedRef<Schedule[]> {
  const store = useProjectStore()
  const taskStore = useTaskStore()
  return computed(() => taskStore.schedules.filter(s => scheduleInWorkspace(
    s,
    store.activeWorkspace,
    store.chats,
    store.projects,
  )))
}
