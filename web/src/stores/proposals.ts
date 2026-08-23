import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import type {
  ProposalsResponse,
  ProposalRow,
  ProposalBatchResponse,
  ProposalDismissOlderResponse,
} from '../lib/types'

/**
 * Proposal-review queue. Mirrors the housekeeping store's best-effort
 * contract: a failed fetch leaves the list empty rather than throwing. The
 * batch endpoints do not return the updated list (they return per-row
 * results), so after any mutation the store re-fetches to reflect what the
 * server actually kept.
 */
export const useProposalsStore = defineStore('proposals', () => {
  const rows = ref<ProposalRow[]>([])
  const loading = ref(false)
  const busyIds = ref<Set<string>>(new Set())
  const busy = computed(() => busyIds.value.size > 0)
  const error = ref('')

  function isBusy(id: string): boolean {
    return busyIds.value.has(id)
  }

  function setBusy(id: string, on: boolean) {
    const next = new Set(busyIds.value)
    if (on) next.add(id)
    else next.delete(id)
    busyIds.value = next
  }

  function setBusyMany(ids: string[], on: boolean) {
    const next = new Set(busyIds.value)
    for (const id of ids) {
      if (on) next.add(id)
      else next.delete(id)
    }
    busyIds.value = next
  }

  // Review-view filter and selection state. It lives here, not in the panel,
  // because the sidebar owns the filter controls the way it does on the memory
  // map, and the panel owns the list they filter. Two copies of "which kind is
  // showing" would let the chip row and the list disagree.
  const kindFilter = ref('all')
  const search = ref('')
  const selected = ref<Set<string>>(new Set())

  /** Rows belonging to one workspace.
   *
   * A row with no workspace is install-wide and shows under whichever is
   * active, because it applies to all of them. No active workspace yet — a
   * single-workspace install, or the store still loading — shows everything;
   * hiding every row until a switcher reports a selection would read as an
   * empty queue.
   */
  function scopedRows(workspace: string): ProposalRow[] {
    return rows.value.filter(r => !workspace || !r.workspace || r.workspace === workspace)
  }

  /** Rows the list should render: workspace scope, then kind, then search. */
  function visibleRows(workspace: string): ProposalRow[] {
    const needle = search.value.trim().toLowerCase()
    return scopedRows(workspace).filter(r =>
      (kindFilter.value === 'all' || r.kind === kindFilter.value)
      && (!needle
        || (r.text ?? '').toLowerCase().includes(needle)
        || (r.path ?? '').toLowerCase().includes(needle)),
    )
  }

  /** Kind tallies for the sidebar chips, over the workspace scope only — so a
   * chip's count does not change when you click another chip. */
  function kindCounts(workspace: string): { kind: string; count: number }[] {
    const tally = new Map<string, number>()
    for (const row of scopedRows(workspace)) {
      tally.set(row.kind, (tally.get(row.kind) ?? 0) + 1)
    }
    return [...tally.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([kind, count]) => ({ kind, count }))
  }

  function resetFilters() {
    kindFilter.value = 'all'
    search.value = ''
  }

  async function fetch() {
    loading.value = true
    error.value = ''
    try {
      const data = await api.get<ProposalsResponse>('/api/proposals')
      rows.value = data.rows ?? []
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Could not load proposals'
    } finally {
      loading.value = false
    }
  }

  /** `workspace` names the destination for a re-home accept.
   *
   * A row whose tags name two workspaces is a question only the operator can
   * answer, and the picker used to throw the answer away: every candidate button
   * called accept with no destination, so the server had nothing to move into.
   */
  async function act(id: string, action: 'accept' | 'dismiss', workspace = ''): Promise<{ ok: boolean; error?: string }> {
    setBusy(id, true)
    error.value = ''
    try {
      const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
      await api.post<ProposalBatchResponse>(`/api/proposals/${id}/${action}${query}`)
      await fetch()
      return { ok: true }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Action failed'
      error.value = msg
      return { ok: false, error: msg }
    } finally {
      setBusy(id, false)
    }
  }

  /** `workspace` names the destination for re-home rows in the selection. */
  async function batch(ids: string[], action: 'accept' | 'dismiss', workspace = '') {
    if (!ids.length) return
    setBusyMany(ids, true)
    error.value = ''
    try {
      await api.post<ProposalBatchResponse>('/api/proposals/batch', {
        action, ids, ...(workspace ? { workspace } : {}),
      })
      await fetch()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Batch action failed'
    } finally {
      setBusyMany(ids, false)
    }
  }

  async function dismissOlderThan(date: string) {
    const key = '__dismissOlderThan__'
    setBusy(key, true)
    error.value = ''
    try {
      await api.post<ProposalDismissOlderResponse>(
        `/api/proposals/dismiss-older-than?date=${encodeURIComponent(date)}`,
      )
      await fetch()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Dismiss failed'
    } finally {
      setBusy(key, false)
    }
  }

  return {
    rows, loading, busy, busyIds, isBusy, setBusy, setBusyMany, error, fetch, act, batch, dismissOlderThan,
    kindFilter, search, selected,
    scopedRows, visibleRows, kindCounts, resetFilters,
  }
})
