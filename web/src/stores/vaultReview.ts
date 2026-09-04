import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import type {
  VaultReviewCandidate,
  VaultReviewResponse,
  VaultTrashedNote,
} from '../lib/types'

export type VaultReviewDisposition = 'keep' | 'improve_link' | 'defer'

function reviewUrl(workspace: string, includeTrashed: boolean): string {
  const query = `workspace=${encodeURIComponent(workspace)}${includeTrashed ? '&include=trashed' : ''}`
  return `/api/vault/review?${query}`
}

/**
 * Stale-note retirement queue. Mirrors the proposals store's best-effort
 * contract: a failed fetch leaves the list as it was rather than throwing,
 * and every mutation re-fetches so the panel reflects what the server kept.
 *
 * Scoped by the server, not the client: every request names the workspace
 * (`GET /api/vault/review` requires it) and the store remembers which scope
 * it holds, so a response for a workspace the user has left cannot overwrite
 * the one they are looking at.
 */
export const useVaultReviewStore = defineStore('vaultReview', () => {
  const candidates = ref<VaultReviewCandidate[]>([])
  const trashed = ref<VaultTrashedNote[]>([])
  const loading = ref(false)
  const busyIds = ref<Set<string>>(new Set())
  const busy = computed(() => busyIds.value.size > 0)
  const error = ref('')
  const loadedWorkspace = ref<string | null>(null)
  let fetchPromise: Promise<void> | null = null
  let fetchWorkspace: string | null = null
  /** Ticket for the newest in-flight list request; older responses are dropped. */
  let fetchSeq = 0

  function isBusy(id: string): boolean {
    return busyIds.value.has(id)
  }

  function setBusy(id: string, on: boolean) {
    const next = new Set(busyIds.value)
    if (on) next.add(id)
    else next.delete(id)
    busyIds.value = next
  }

  /**
   * Load the queue for one workspace. `force` starts a fresh request even
   * when one is in flight — the refresh after a mutation must never join a
   * GET issued before its own POST, or the decided row stays queued as if
   * nothing happened. A request for a different workspace never joins either:
   * otherwise flipping workspaces mid-flight would leave the new scope
   * showing the old scope's rows with no refetch until manual refresh.
   */
  async function fetch(workspace: string, opts?: { force?: boolean }): Promise<void> {
    if (!workspace) return
    if (fetchPromise && !opts?.force && fetchWorkspace === workspace) return fetchPromise
    const seq = ++fetchSeq
    fetchWorkspace = workspace
    const request = (async () => {
      loading.value = true
      error.value = ''
      try {
        const data = await api.get<VaultReviewResponse>(reviewUrl(workspace, true))
        if (seq !== fetchSeq) return
        candidates.value = data.candidates ?? []
        trashed.value = data.trashed ?? []
        loadedWorkspace.value = workspace
      } catch (e) {
        if (seq !== fetchSeq) return
        error.value = e instanceof Error ? e.message : 'Could not load retirement candidates'
      } finally {
        if (seq === fetchSeq) loading.value = false
      }
    })()
    fetchPromise = request
    try {
      await request
    } finally {
      if (fetchPromise === request) fetchPromise = null
    }
  }

  /** Run one mutation and refresh the queue it changes. */
  async function mutate(workspace: string, id: string, body: Record<string, unknown>): Promise<boolean> {
    setBusy(id, true)
    error.value = ''
    try {
      await api.post<{ ok: boolean }>(reviewUrl(workspace, false), body)
      await fetch(workspace, { force: true })
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Action failed'
      return false
    } finally {
      setBusy(id, false)
    }
  }

  /** Record keep / improve_link / defer. Trash/restore/delete are separate actions. */
  async function decide(
    workspace: string,
    id: string,
    disposition: VaultReviewDisposition,
    deferDays = 7,
  ): Promise<boolean> {
    return mutate(workspace, id, {
      action: 'decide',
      candidate_id: id,
      disposition,
      ...(disposition === 'defer' ? { defer_days: deferDays } : {}),
    })
  }

  /** Retire a note into the reversible 30-day trash. */
  async function trash(workspace: string, id: string): Promise<boolean> {
    return mutate(workspace, id, { action: 'trash', candidate_id: id })
  }

  /** Bring a trashed note back to its original path. */
  async function restore(workspace: string, id: string): Promise<boolean> {
    return mutate(workspace, id, { action: 'restore', candidate_id: id })
  }

  /** Permanently delete a trashed note. The server requires the exact
   * candidate id as confirmation; the panel collects the explicit confirm
   * before calling. */
  async function remove(workspace: string, id: string): Promise<boolean> {
    return mutate(workspace, id, { action: 'delete', candidate_id: id, confirm: id })
  }

  return {
    candidates,
    trashed,
    loading,
    busy,
    busyIds,
    isBusy,
    setBusy,
    error,
    loadedWorkspace,
    fetch,
    decide,
    trash,
    restore,
    remove,
  }
})
