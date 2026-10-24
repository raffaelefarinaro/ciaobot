import { computed, reactive, ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage } from '../lib/errorMessage'
import { onboardingProgress, type OnboardingTarget, type OnboardingReceipt } from '../lib/onboarding'
import type { HandoverResponse, MemoryHandover } from '../lib/types'
import { useProjectStore } from '../stores/projects'

export function useOnboardingPreferences(target: OnboardingTarget, workspace: string) {
  const draft = reactive({ memoryHandover: 'off' as MemoryHandover, acknowledged: false })
  const savedHandover = ref<MemoryHandover>('off')
  const actingAvailable = ref(false)
  const pending = ref(false)
  const error = ref<string | null>(null)
  const store = useProjectStore()
  let eligible = false
  let loaded = false
  const dirty = computed(() => loaded && draft.memoryHandover !== savedHandover.value)

  async function load() {
    pending.value = true; error.value = null
    try {
      const [handover, receipt] = await Promise.all([
        api.get<HandoverResponse>(`/api/proposals/handover?workspace=${encodeURIComponent(workspace)}`),
        api.get<OnboardingReceipt>('/api/onboarding'),
      ])
      savedHandover.value = handover.handover
      actingAvailable.value = handover.acting_available
      // Any target whose own journey is still running may be offered the draft:
      // an installation acting on its primary workspace as well as a new workspace.
      eligible = onboardingProgress(receipt, target)?.status === 'in_progress'
      draft.memoryHandover = eligible && handover.handover === 'off' ? 'all' : handover.handover
      draft.acknowledged = false
      loaded = true
    } catch (reason) { error.value = apiErrorMessage(reason, 'Could not read memory handover settings.') }
    finally { pending.value = false }
  }

  async function saveMemoryHandover(): Promise<boolean> {
    if (!loaded || pending.value || !draft.acknowledged || error.value) return false
    if (!dirty.value) return true
    pending.value = true; error.value = null
    const value = draft.memoryHandover
    try {
      await store.updateWorkspace(workspace, { memory_handover: value })
      savedHandover.value = value
      return true
    } catch (reason) { error.value = apiErrorMessage(reason, 'The memory handover choice could not be saved.'); return false }
    finally { pending.value = false }
  }
  return { load, draft, savedHandover, actingAvailable, dirty, pending, error, saveMemoryHandover }
}
