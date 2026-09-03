import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import type {
  ProposalsResponse,
  ProposalRow,
  ProposalBatchResponse,
  ProposalDismissOlderResponse,
  ProposalHistoryResponse,
  ProposalHistoryRow,
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
  const loaded = ref(false)
  let fetchPromise: Promise<void> | null = null
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

  // Queue vs. decision history. Lives here rather than in the panel because
  // the panel's tab bar sets it while `fetch` reads it, to refresh a history
  // tab that is already open after a queue mutation.
  const view = ref<'queue' | 'history'>('queue')
  const historyRows = ref<ProposalHistoryRow[]>([])
  const historyLoading = ref(false)
  const historyLoaded = ref(false)
  const historyTruncated = ref(false)
  /** The server refused to widen the page further; "show more" would no-op. */
  const historyAtMax = ref(false)
  /** How many decisions exist in scope, which can exceed the served page. */
  const historyTotal = ref(0)
  /** History failures get their own slot: `error` belongs to the queue, and
   * clearing it here wiped an accept failure the operator had not read yet. */
  const historyError = ref('')
  const historyActionFilter = ref<'all' | 'accepted' | 'dismissed'>('all')
  const historyActorFilter = ref<'all' | 'pwa' | 'agent' | 'auto'>('all')
  /** How many history rows to ask the server for; grows on "show more". */
  const historyLimit = ref(200)
  /** Which workspace the loaded history covers, so a switch refetches. The
   * server pages the newest N rows, so filtering a global page client-side
   * starved a quiet workspace on a busy install: its decisions fell outside
   * the window and its History tab read "No decisions yet." */
  const historyWorkspace = ref<string | null>(null)
  let historyFetchPromise: Promise<boolean> | null = null
  /** Ticket for the newest in-flight history request; older responses are dropped. */
  let historySeq = 0

  /** Mark the loaded history stale. Called after a mutation, not on every
   * queue load: a plain tab switch re-fetched the whole ledger for nothing.
   *
   * Refetches whenever something is already displaying the ledger - the
   * History tab, or the Queue tab's count badge, which renders only once
   * history has loaded. Clearing `historyLoaded` without refetching made that
   * badge disappear on every accept/dismiss and stay gone until the tab was
   * reopened. */
  function invalidateHistory() {
    const wasLoaded = historyLoaded.value
    historyLoaded.value = false
    if (view.value === 'history' || wasLoaded) void fetchHistory({ force: true })
  }

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
    historyActionFilter.value = 'all'
    historyActorFilter.value = 'all'
  }

  function pruneSelected() {
    if (!selected.value.size) return
    const live = new Set(rows.value.map(r => r.id))
    let changed = false
    for (const id of selected.value) {
      if (!live.has(id)) { changed = true; break }
    }
    if (changed) {
      selected.value = new Set([...selected.value].filter(id => live.has(id)))
    }
  }

  /**
   * Load the queue. `force` starts a fresh request even when one is in flight.
   *
   * Joining an in-flight request is right for concurrent *readers* (the panel
   * and the sidebar mounting together), but wrong for the refresh that follows
   * a mutation: an accept whose refresh joined a GET issued before its own POST
   * was handed the pre-mutation snapshot, so the accepted row stayed in the
   * queue as if nothing had happened. A forced request also takes a ticket, so
   * the older, pre-mutation response cannot overwrite its result either.
   */
  async function fetch(opts?: { force?: boolean }): Promise<void> {
    if (fetchPromise && !opts?.force) return fetchPromise
    const seq = ++fetchSeq
    const request = (async () => {
      loading.value = true
      error.value = ''
      try {
        const data = await api.get<ProposalsResponse>('/api/proposals')
        if (seq !== fetchSeq) return
        rows.value = data.rows ?? []
        loaded.value = true
        pruneSelected()
      } catch (e) {
        if (seq !== fetchSeq) return
        error.value = e instanceof Error ? e.message : 'Could not load proposals'
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

  async function ensureLoaded(): Promise<void> {
    // Tests and callers may hydrate rows directly before mounting a consumer;
    // treat that as an already-loaded snapshot rather than replacing it with a
    // background request.
    if (loaded.value || rows.value.length > 0) {
      loaded.value = true
      return
    }
    await fetch()
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
      await fetch({ force: true })
      invalidateHistory()
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
      await fetch({ force: true })
      invalidateHistory()
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
      await fetch({ force: true })
      invalidateHistory()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Dismiss failed'
    } finally {
      setBusy(key, false)
    }
  }

  /**
   * Load the decision history. Same de-dup/ticket shape as `fetch`: a plain
   * mount joins an in-flight request, but the refresh a mutation triggers
   * (see `fetch` above) forces a fresh one so it is never handed a
   * pre-mutation snapshot.
   */
  async function fetchHistory(
    opts?: { force?: boolean; limit?: number; workspace?: string },
  ): Promise<boolean> {
    if (opts?.limit) historyLimit.value = opts.limit
    if (opts?.workspace !== undefined) historyWorkspace.value = opts.workspace
    if (historyFetchPromise && !opts?.force) return historyFetchPromise
    const seq = ++historySeq
    const requested = historyLimit.value
    const workspace = historyWorkspace.value ?? ''
    const request = (async () => {
      historyLoading.value = true
      historyError.value = ''
      try {
        const query = new URLSearchParams({ limit: String(requested) })
        // Scope server-side: the response is the newest N rows, so filtering a
        // global page down to one workspace can leave nothing to show.
        if (workspace) query.set('workspace', workspace)
        const data = await api.get<ProposalHistoryResponse>(
          `/api/proposals/history?${query.toString()}`,
        )
        if (seq !== historySeq) return false
        historyRows.value = data.rows ?? []
        historyTotal.value = data.total ?? historyRows.value.length
        historyTruncated.value = Boolean(data.truncated)
        // The endpoint always sends this. The old `data.limit < requested`
        // fallback could not fire — the server reports the limit it served,
        // not the one it refused — so termination now rests on the row-count
        // backstop in `loadMoreHistory`, which needs no cooperation at all.
        historyAtMax.value = Boolean(data.at_max)
        // Track the page the server actually served, not what we asked for.
        if (data.limit) historyLimit.value = data.limit
        historyLoaded.value = true
        return true
      } catch (e) {
        if (seq !== historySeq) return false
        historyError.value = e instanceof Error ? e.message : 'Could not load proposal history'
        return false
      } finally {
        if (seq === historySeq) historyLoading.value = false
      }
    })()
    historyFetchPromise = request
    try {
      return await request
    } finally {
      if (historyFetchPromise === request) historyFetchPromise = null
    }
  }

  /** Whether a wider page would actually return anything new. */
  const historyCanLoadMore = computed(() => historyTruncated.value && !historyAtMax.value)

  /** Whether any client-side filter is narrowing the ledger.
   *
   * Lives here because both the list (which offers the reset) and the panel's
   * tab badge (which must not report a filtered count as the total) need the
   * same answer. */
  const historyFiltersActive = computed(
    () => kindFilter.value !== 'all'
      || Boolean(search.value)
      || historyActionFilter.value !== 'all'
      || historyActorFilter.value !== 'all',
  )

  /** Ask the server for more history rows and refresh with the wider limit. */
  async function loadMoreHistory(): Promise<void> {
    if (!historyCanLoadMore.value) return
    const before = historyRows.value.length
    const applied = await fetchHistory({ force: true, limit: historyLimit.value + 200 })
    // Termination backstop, independent of what the server reports: a wider
    // page that came back no longer has nothing left to give, so stop asking.
    // Without it a server that got `at_max` wrong left "show more" refetching
    // the same page on every click, forever.
    //
    // Only when THIS request is the one that applied. Two ways it is not, and
    // both leave `historyRows` at its previous length — indistinguishable from
    // "nothing more to give": it failed (a transient 500 hid "show more"
    // permanently, with the footer claiming the cap was reached), or a newer
    // request superseded it, which `fetchHistory` reports by bailing on the
    // seq check before touching any state. The second is reachable in normal
    // use: a queue mutation forces a refresh while "show more" is in flight,
    // and it also clears `historyError`, so testing the error alone missed it.
    if (applied && historyRows.value.length <= before) historyAtMax.value = true
  }

  async function ensureHistoryLoaded(workspace?: string): Promise<void> {
    const scope = workspace ?? historyWorkspace.value ?? ''
    // A workspace switch invalidates the loaded page as surely as a mutation:
    // the server scoped it to the previous workspace.
    if (historyLoaded.value && historyWorkspace.value === scope) return
    const changed = historyWorkspace.value !== scope
    await fetchHistory({ workspace: scope, force: changed })
  }

  /** History rows in scope for one workspace, same rule as `scopedRows`. */
  function scopedHistory(workspace: string): ProposalHistoryRow[] {
    return historyRows.value.filter(r => !workspace || !r.workspace || r.workspace === workspace)
  }

  /** History rows the list should render: scope, then kind/action/actor/search. */
  function visibleHistory(workspace: string): ProposalHistoryRow[] {
    const needle = search.value.trim().toLowerCase()
    return scopedHistory(workspace).filter(r =>
      (kindFilter.value === 'all' || r.kind === kindFilter.value)
      && (historyActionFilter.value === 'all' || r.action === historyActionFilter.value)
      && (historyActorFilter.value === 'all' || r.via === historyActorFilter.value)
      && (!needle
        || (r.text ?? '').toLowerCase().includes(needle)
        || (r.destination ?? '').toLowerCase().includes(needle)
        || (r.source ?? '').toLowerCase().includes(needle)),
    )
  }

  return {
    rows, loading, loaded, busy, busyIds, isBusy, setBusy, setBusyMany, error, fetch, ensureLoaded, act, batch, dismissOlderThan,
    kindFilter, search, selected,
    scopedRows, visibleRows, kindCounts, resetFilters,
    view, historyRows, historyLoading, historyLoaded, historyTruncated, historyLimit,
    historyAtMax, historyTotal, historyCanLoadMore, historyFiltersActive, historyError, historyWorkspace,
    historyActionFilter, historyActorFilter,
    fetchHistory, ensureHistoryLoaded, loadMoreHistory, invalidateHistory, scopedHistory, visibleHistory,
  }
})
