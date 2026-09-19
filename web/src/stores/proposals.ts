import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import type {
  ProposalsResponse,
  ProposalRow,
  ProposalBatchResponse,
  ProposalBatchSummary,
  ProposalDismissOlderResponse,
  ProposalHistoryResponse,
  ProposalHistoryRow,
  ProposalPreview,
  ProposalPreviewResponse,
  MemoryReceiptDetail,
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
  /** Action failures only (an accept/dismiss that refused). Kept separate from
   * `loadError` so a list refresh can never erase an unread action failure. */
  const error = ref('')
  /** List-load failures only. Separate from `error` for the same reason the
   * History ledger has `historyError`: a failed GET is not the outcome of an
   * action, and clearing it here used to wipe an accept error nobody had read.
   * When set while `loaded` is true, the rows on screen are the last
   * successfully loaded snapshot and must be labelled stale, not empty. */
  const loadError = ref('')
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

  // -- Accept previews -------------------------------------------------------
  //
  // A queued bullet says what was noticed, not what accepting it writes: the
  // promotion reconciles against whatever the destination holds now. The
  // server computes that replacement (`GET /api/proposals/{id}/preview`) and
  // pins the destination revision it was computed against; that revision goes
  // back with the accept so a destination which moved in between is refused
  // rather than overwritten. Keeping the previews here (not in the panel) is
  // what lets the batch bar send revisions for rows whose cards were opened.
  const previews = ref<Record<string, ProposalPreview>>({})
  const previewLoading = ref<Set<string>>(new Set())
  const previewErrors = ref<Record<string, string>>({})
  /** Rows whose last accept was refused because the destination had moved.
   * Cleared when the refreshed preview is confirmed or the card is closed. */
  const conflictIds = ref<Set<string>>(new Set())
  /** Per-destination roll-up of the last batch, for the panel's summary line. */
  const lastBatchSummary = ref<ProposalBatchSummary[]>([])

  function isPreviewLoading(id: string): boolean {
    return previewLoading.value.has(id)
  }

  function setPreviewLoading(id: string, on: boolean) {
    const next = new Set(previewLoading.value)
    if (on) next.add(id)
    else next.delete(id)
    previewLoading.value = next
  }

  /** Load (or reload) what accepting one row would write.
   *
   * `text` previews an edited wording against the same current destination,
   * which is what the card's "edit suggestion" sends on every change. A failed
   * load leaves no preview, and the card says so rather than showing a stale
   * one as if it were current.
   */
  async function loadPreview(id: string, text = ''): Promise<ProposalPreview | null> {
    setPreviewLoading(id, true)
    const next = { ...previewErrors.value }
    delete next[id]
    previewErrors.value = next
    try {
      const query = text ? `?text=${encodeURIComponent(text)}` : ''
      const reply = await api.get<ProposalPreviewResponse>(
        `/api/proposals/${id}/preview${query}`,
      )
      const preview = reply?.preview ?? null
      if (preview) previews.value = { ...previews.value, [id]: preview }
      return preview
    } catch (e) {
      previewErrors.value = {
        ...previewErrors.value,
        [id]: e instanceof Error ? e.message : 'Could not read the destination',
      }
      const without = { ...previews.value }
      delete without[id]
      previews.value = without
      return null
    } finally {
      setPreviewLoading(id, false)
    }
  }

  function dropPreview(id: string) {
    if (id in previews.value) {
      const next = { ...previews.value }
      delete next[id]
      previews.value = next
    }
    if (id in previewErrors.value) {
      const next = { ...previewErrors.value }
      delete next[id]
      previewErrors.value = next
    }
    if (conflictIds.value.has(id)) {
      const next = new Set(conflictIds.value)
      next.delete(id)
      conflictIds.value = next
    }
  }

  // -- Change receipts (History) ---------------------------------------------
  //
  // History rows carry a `change` pointer when the receipt protocol performed
  // the decision; the images themselves are fetched per row, on expand, since
  // a page of 200 decisions would otherwise ship 200 before/after bodies
  // nobody opened.
  const receipts = ref<Record<string, MemoryReceiptDetail>>({})
  const receiptErrors = ref<Record<string, string>>({})
  const receiptLoading = ref<Set<string>>(new Set())

  function isReceiptLoading(id: string): boolean {
    return receiptLoading.value.has(id)
  }

  async function loadReceipt(id: string, workspace = ''): Promise<MemoryReceiptDetail | null> {
    if (receipts.value[id]) return receipts.value[id]
    const busySet = new Set(receiptLoading.value)
    busySet.add(id)
    receiptLoading.value = busySet
    try {
      const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
      const detail = await api.get<MemoryReceiptDetail>(`/api/memory/receipts/${id}${query}`)
      if (detail) receipts.value = { ...receipts.value, [id]: detail }
      return detail ?? null
    } catch (e) {
      receiptErrors.value = {
        ...receiptErrors.value,
        [id]: e instanceof Error ? e.message : 'Could not read the change',
      }
      return null
    } finally {
      const next = new Set(receiptLoading.value)
      next.delete(id)
      receiptLoading.value = next
    }
  }

  /** Reverse one receipt. The server refuses (409) when the destination moved
   * since the operation, because restoring the before image would delete an
   * unrelated later fact; that refusal is surfaced verbatim rather than
   * retried. */
  async function undoReceipt(id: string, workspace = ''): Promise<{ ok: boolean; error?: string }> {
    setBusy(id, true)
    const next = { ...receiptErrors.value }
    delete next[id]
    receiptErrors.value = next
    try {
      const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
      await api.post(`/api/memory/receipts/${id}/undo${query}`)
      // The change is reversed, so the cached image describes an operation that
      // no longer holds; History refetches and the row re-reads it.
      const without = { ...receipts.value }
      delete without[id]
      receipts.value = without
      invalidateHistory()
      await fetch({ force: true })
      return { ok: true }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Undo failed'
      receiptErrors.value = { ...receiptErrors.value, [id]: msg }
      return { ok: false, error: msg }
    } finally {
      setBusy(id, false)
    }
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
      // Only the list-load slot is cleared here. `error` belongs to actions.
      loadError.value = ''
      try {
        const data = await api.get<ProposalsResponse>('/api/proposals')
        if (seq !== fetchSeq) return
        rows.value = data.rows ?? []
        loaded.value = true
        loadError.value = ''
        pruneSelected()
      } catch (e) {
        if (seq !== fetchSeq) return
        // Rows are left untouched: a refresh that fails keeps the previous
        // successful snapshot, and `loadError` (plus `loaded`) tells the view
        // it is stale rather than empty.
        loadError.value = e instanceof Error ? e.message : 'Could not load proposals'
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
  async function act(
    id: string,
    action: 'accept' | 'dismiss',
    workspace = '',
    opts?: { expectedRevision?: string; text?: string; reconcile?: boolean },
  ): Promise<{ ok: boolean; error?: string; conflict?: boolean; payload?: unknown }> {
    setBusy(id, true)
    error.value = ''
    try {
      const params = new URLSearchParams()
      if (workspace) params.set('workspace', workspace)
      // Opt-in per click: the server spends one model call reconciling the fact
      // against the region, so a plain accept stays one synchronous write and
      // only a row that was asked to be checked pays for it.
      if (opts?.reconcile) params.set('reconcile', '1')
      const queryString = params.toString()
      const query = queryString ? `?${queryString}` : ''
      const body: Record<string, string> = {}
      // Only sent when a preview was actually shown. Without it the server
      // keeps its pre-preview behaviour, which is what the batch bar and any
      // older client rely on.
      if (opts?.expectedRevision) body.expected_revision = opts.expectedRevision
      if (opts?.text) body.text = opts.text
      await api.post<ProposalBatchResponse>(`/api/proposals/${id}/${action}${query}`, body)
      await fetch({ force: true })
      invalidateHistory()
      return { ok: true }
    } catch (e) {
      const conflict = adoptConflict(id, e)
      const payload = (e as { payload?: unknown } | null)?.payload
      const deferred = Boolean((payload as { deferred?: boolean } | undefined)?.deferred)
      const msg = e instanceof Error ? e.message : 'Action failed'
      // A conflict is not an action failure: the card is already re-rendering
      // with the refreshed preview, and a toast on top of it would say the
      // same thing twice in two places. Nor is a deferral: the caller renders
      // it on the row, with the entries it was weighed against and a retry, and
      // a toast would take all three away the moment they became relevant.
      if (!conflict && !deferred) error.value = msg
      return { ok: false, error: msg, conflict, payload }
    } finally {
      setBusy(id, false)
    }
  }

  /** Take the refreshed preview a 409 carries, so the card can re-render.
   *
   * The whole point of the conflict response is that the operator sees the
   * destination as it is NOW without a second round trip; dropping the payload
   * and refetching would reintroduce the gap the handshake closes. */
  function adoptConflict(id: string, e: unknown): boolean {
    const payload = (e as { payload?: { conflict?: boolean; preview?: ProposalPreview } })?.payload
    if (!payload?.conflict) return false
    if (payload.preview) previews.value = { ...previews.value, [id]: payload.preview }
    conflictIds.value = new Set(conflictIds.value).add(id)
    return true
  }

  /** `workspace` names the destination for re-home rows in the selection. */
  async function batch(ids: string[], action: 'accept' | 'dismiss', workspace = '') {
    if (!ids.length) return
    setBusyMany(ids, true)
    error.value = ''
    lastBatchSummary.value = []
    try {
      // Revisions only for rows whose preview this session actually loaded.
      // A row accepted straight from the list sends none and keeps the
      // unguarded behaviour, rather than being blocked on a card nobody opened.
      const revisions: Record<string, string> = {}
      for (const id of ids) {
        const revision = previews.value[id]?.revision
        if (revision) revisions[id] = revision
      }
      const reply = await api.post<ProposalBatchResponse>('/api/proposals/batch', {
        action, ids, ...(workspace ? { workspace } : {}),
        ...(Object.keys(revisions).length ? { revisions } : {}),
      })
      lastBatchSummary.value = reply?.summary ?? []
      const conflicted = (reply?.results ?? []).filter(r => r.conflict).map(r => r.id)
      if (conflicted.length) conflictIds.value = new Set([...conflictIds.value, ...conflicted])
      // Previews of rows that are gone would otherwise be handed to the next
      // batch as revisions for ids the server no longer knows.
      for (const id of ids) dropPreview(id)
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
    rows, loading, loaded, busy, busyIds, isBusy, setBusy, setBusyMany, error, loadError, fetch, ensureLoaded, act, batch, dismissOlderThan,
    previews, previewErrors, isPreviewLoading, loadPreview, dropPreview, conflictIds, lastBatchSummary,
    receipts, receiptErrors, isReceiptLoading, loadReceipt, undoReceipt,
    kindFilter, search, selected,
    scopedRows, visibleRows, kindCounts, resetFilters,
    view, historyRows, historyLoading, historyLoaded, historyTruncated, historyLimit,
    historyAtMax, historyTotal, historyCanLoadMore, historyFiltersActive, historyError, historyWorkspace,
    historyActionFilter, historyActorFilter,
    fetchHistory, ensureHistoryLoaded, loadMoreHistory, invalidateHistory, scopedHistory, visibleHistory,
  }
})
