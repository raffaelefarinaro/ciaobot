import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import {
  clearChecklistDismissed,
  countReadyProviders,
  gettingStartedProgress,
  isChecklistDismissed,
  markChecklistDismissed,
  type GettingStartedState,
} from '../lib/gettingStarted'
import { useProjectStore } from './projects'
import { useTaskStore } from './tasks'

export const useGettingStartedStore = defineStore('gettingStarted', () => {
  const dismissed = ref(isChecklistDismissed())
  const providerReadyCount = ref(0)
  const providerStatusLoaded = ref(false)

  async function fetchProviderStatus() {
    try {
      const status = await api.get<unknown>('/api/setup-status')
      providerReadyCount.value = countReadyProviders(status)
    } catch {
      // Endpoint unreachable: keep 0, the provider item just stays open.
    } finally {
      providerStatusLoaded.value = true
    }
  }

  const state = computed<GettingStartedState>(() => {
    const projects = useProjectStore()
    const tasks = useTaskStore()
    return {
      providerReadyCount: providerReadyCount.value,
      workspaceCount: projects.workspaceOptions.length,
      userProjectCount: projects.projects.filter(p => !p.is_auto && !p.is_system).length,
      scheduleCount: tasks.schedules.length,
      activeChatCount: projects.chats.filter(c => c.last_activity_at).length,
    }
  })

  const progress = computed(() => gettingStartedProgress(state.value))

  function dismiss() {
    dismissed.value = true
    markChecklistDismissed()
  }

  function restore() {
    dismissed.value = false
    clearChecklistDismissed()
  }

  return {
    dismissed,
    providerReadyCount,
    providerStatusLoaded,
    fetchProviderStatus,
    state,
    progress,
    dismiss,
    restore,
  }
})
