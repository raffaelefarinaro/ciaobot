import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type {
  VaultReviewCandidate,
  VaultReviewResponse,
  VaultTrashedNote,
  VaultClearedNote,
  VaultReviewDecisionResult,
} from '../lib/types'

export type VaultReviewDisposition = 'keep'

/**
 * The union straight off the wire type, not a re-spelling and not `string`:
 * typing the reason helpers as `string` would put back the exact hole the
 * union exists to close — a typo in one of the literals below would compile
 * clean and silently disable the notice.
 */
type StampStatus = VaultReviewDecisionResult['stamp_status']

function reviewUrl(workspace: string, includeLists: boolean): string {
  const query = `workspace=${encodeURIComponent(workspace)}${includeLists ? '&include=trashed,cleared' : ''}`
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
  // Notes cleared with "Still true" that are still in the vault. The panel's
  // only route back into the queue: a keep is suppressed by content hash, so
  // without this, undoing one meant editing the note.
  const cleared = ref<VaultClearedNote[]>([])
  const loading = ref(false)
  const busyIds = ref<Set<string>>(new Set())
  const error = ref('')
  const loadError = ref('')
  // Set when an action succeeded but did not do everything its label claims —
  // "Still true" on a note with no frontmatter clears the row without writing
  // a date. Silence there left the button looking broken: the row went away
  // while `memory-audit` and the Memory Map badge kept flagging the note.
  const notice = ref('')
  const loadedWorkspace = ref<string | null>(null)
  let fetchPromise: Promise<void> | null = null
  let fetchWorkspace: string | null = null
  /** Ticket for the newest in-flight list request; older responses are dropped. */
  let fetchSeq = 0
  /** Newest mutation response whose returned queue has been adopted. */
  let latestMutationSnapshot = 0
  let mutationSeq = 0

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
      loadError.value = ''
      try {
        const data = await api.get<VaultReviewResponse>(reviewUrl(workspace, true))
        if (seq !== fetchSeq) return
        candidates.value = data.candidates ?? []
        trashed.value = data.trashed ?? []
        cleared.value = data.cleared ?? []
        loadedWorkspace.value = workspace
      } catch (e) {
        if (seq !== fetchSeq) return
        loadError.value = e instanceof Error ? e.message : 'Could not load retirement candidates'
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
   *
   * The per-decision `result` is *returned*, never parked in a store ref: two
   * decisions can be in flight at once (the busy set is per-candidate id, so
   * "Still true" on row A and row B in quick succession both POST), and a
   * shared slot lets the second response land between the first one arriving
   * and its caller reading it. Each call now reads only its own answer.
   */
  async function mutate(
    workspace: string,
    id: string,
    body: Record<string, unknown>,
  ): Promise<{ ok: boolean; result: VaultReviewDecisionResult | null }> {
    const mutationTicket = ++mutationSeq
    setBusy(id, true)
    error.value = ''
    notice.value = ''
    let result: VaultReviewDecisionResult | null = null
    try {
      const data = await api.post<VaultReviewResponse & { ok: boolean }>(
        reviewUrl(workspace, false), body,
      )
      result = data?.result ?? null
      if (data && Array.isArray(data.candidates)) {
        // Only adopt it for the workspace we asked about: the user may have
        // switched scopes while the POST was in flight, and a late response
        // must not repaint the new scope with the old one's rows.
        if (
          (loadedWorkspace.value === workspace || loadedWorkspace.value === null)
          && mutationTicket >= latestMutationSnapshot
        ) {
          latestMutationSnapshot = mutationTicket
          // Take a fresh list ticket so a GET issued before this POST landed
          // cannot repaint the pre-mutation queue over it — the decided row
          // would come back as if nothing had happened. The old code got this
          // for free from the `fetch(..., { force: true })` it no longer runs.
          //
          // Only a GET for *this* scope: a fetch the user triggered by
          // switching workspaces must still land, or the new scope would sit
          // on the old one's rows with nothing left to refetch it. And clear
          // `loading` here — the orphaned request's own `finally` is gated on
          // still holding the ticket, so nobody else ever would, and the
          // panel's refresh button would stay disabled reading "loading…".
          if (fetchPromise && fetchWorkspace === workspace) {
            ++fetchSeq
            loading.value = false
          }
          candidates.value = data.candidates
          trashed.value = data.trashed ?? []
          cleared.value = data.cleared ?? []
          loadedWorkspace.value = workspace
          loadError.value = ''
        }
      } else {
        await fetch(workspace, { force: true })
      }
      return { ok: true, result }
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Action failed'
      return { ok: false, result: null }
    } finally {
      setBusy(id, false)
    }
  }

  /**
   * Why the note was not stamped, phrased for a toast — or '' when it was
   * stamped, was already current, or the disposition never stamps.
   *
   * One message for all three failures said "no frontmatter to stamp", which
   * is wrong for a note that could not be read or decoded — and, before the
   * BOM fix, wrong for a note whose frontmatter was perfectly good.
   */
  function stampFailureReason(status: StampStatus): string {
    if (status === 'no_frontmatter') return 'this note has no frontmatter to stamp'
    if (status === 'not_utf8') return 'this note is not valid UTF-8, so it was left untouched'
    if (status === 'unreadable') return 'this note could not be read'
    return ''
  }

  /**
   * What the user can still do about it, when there is something. The generic
   * "verified date is unchanged" covers all three failures but drops the one
   * piece of advice that was actionable: a note with no frontmatter keeps
   * coming back to the queue until it gets some.
   */
  function stampFailureHint(status: StampStatus): string {
    if (status === 'no_frontmatter') {
      return ' It will keep showing as needing review until you add frontmatter to it.'
    }
    return ''
  }

  /** Record a keep. Trash/restore/delete are separate actions. */
  async function decide(
    workspace: string,
    id: string,
    disposition: VaultReviewDisposition,
  ): Promise<boolean> {
    const { ok, result } = await mutate(
      workspace, id, { action: 'decide', candidate_id: id, disposition },
    )
    // Still gated on the row's own id, now as a server-answer check rather
    // than a race guard: the result belongs to this call, so it can only
    // mismatch if the engine answered about a different candidate.
    if (ok && disposition === 'keep' && result?.previous_candidate_id === id) {
      const reason = stampFailureReason(result.stamp_status)
      if (reason) {
        notice.value =
          `The row is cleared, but ${reason}, so this note's verified date is unchanged.`
          + stampFailureHint(result.stamp_status)
      }
    }
    return ok
  }

  /** Retire a note into the reversible trash. */
  async function trash(workspace: string, id: string): Promise<boolean> {
    return (await mutate(workspace, id, { action: 'trash', candidate_id: id })).ok
  }

  /** Bring a trashed note back to its original path. */
  async function restore(workspace: string, id: string): Promise<boolean> {
    return (await mutate(workspace, id, { action: 'restore', candidate_id: id })).ok
  }

  /** Put a cleared note back in the queue, undoing its `keep`. */
  async function reopen(workspace: string, id: string): Promise<boolean> {
    return (await mutate(workspace, id, { action: 'reopen', candidate_id: id })).ok
  }

  /** Permanently delete a trashed note. The server requires the exact
   * candidate id as confirmation; the panel collects the explicit confirm
   * before calling. */
  async function remove(workspace: string, id: string): Promise<boolean> {
    return (await mutate(workspace, id, { action: 'delete', candidate_id: id, confirm: id })).ok
  }

  return {
    candidates,
    trashed,
    cleared,
    reopen,
    loading,
    isBusy,
    error,
    loadError,
    notice,
    loadedWorkspace,
    fetch,
    ensureLoaded,
    decide,
    trash,
    restore,
    remove,
  }
})
