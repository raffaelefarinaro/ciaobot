import { defineStore } from 'pinia'
import { ref } from 'vue'
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
   * Load the queue for one workspace unless it is already loaded.
   *
   * `fetch` only joins an *in-flight* request, so callers that fire on every
   * mount or view change (the Retirement badge watcher, the panel's own
   * `onMounted`) re-ran the heaviest read in the app each time — the endpoint
   * scans every note in the vault three times. The two panels are `v-if`
   * siblings, so flipping tabs remounts and refetches. Mirrors
   * `proposals.ensureLoaded()`; the refresh button and the workspace watch
   * still force a real request.
   */
  async function ensureLoaded(workspace: string): Promise<void> {
    if (!workspace) return
    if (loadedWorkspace.value === workspace) return
    await fetch(workspace)
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

  /**
   * Run one mutation and adopt the queue the server rebuilt for it.
   *
   * The POST already regenerates the queue — the readable projection is
   * pending-only and has to reflect the mutation — and now returns it, so this
   * no longer re-GETs. That mattered: `generate_candidates` reads every note in
   * the vault three times, so a follow-up fetch made a single row click cost a
   * whole extra scan of the vault (~0.37s and ~3500 file reads on a 1178-note
   * vault). Falls back to a fetch if an older engine answers without the
   * snapshot, so a stale PWA against a new engine (or the reverse) still
   * refreshes.
   */
  async function mutate(workspace: string, id: string, body: Record<string, unknown>): Promise<boolean> {
    setBusy(id, true)
    error.value = ''
    try {
      const data = await api.post<VaultReviewResponse & { ok: boolean }>(
        reviewUrl(workspace, false), body,
      )
      if (data && Array.isArray(data.candidates)) {
        // Only adopt it for the workspace we asked about: the user may have
        // switched scopes while the POST was in flight, and a late response
        // must not repaint the new scope with the old one's rows.
        if (loadedWorkspace.value === workspace || loadedWorkspace.value === null) {
          // Take a fresh list ticket so a GET issued before this POST landed
          // cannot repaint the pre-mutation queue over it — the decided row
          // would come back as if nothing had happened. The old code got this
          // for free from the `fetch(..., { force: true })` it no longer runs.
          ++fetchSeq
          candidates.value = data.candidates
          trashed.value = data.trashed ?? []
          loadedWorkspace.value = workspace
        }
      } else {
        await fetch(workspace, { force: true })
      }
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
    isBusy,
    error,
    loadedWorkspace,
    fetch,
    ensureLoaded,
    decide,
    trash,
    restore,
    remove,
  }
})
