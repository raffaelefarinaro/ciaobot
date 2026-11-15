import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref } from 'vue'
import { api } from '../lib/api'
import type {
  ProposalsResponse,
  ProposalRow,
  ProposalBatchResponse,
  ProposalBatchSummary,
  ProposalActionResult,
  ProposalAcceptRefusal,
  ProposalDismissOlderResponse,
  ProposalHistoryResponse,
  ProposalHistoryRow,
  ProposalPreview,
  ProposalPreviewResponse,
  ProposalSkillAcceptResponse,
  MemoryReceiptDetail,
} from '../lib/types'

const INTENT_FAMILY = 'ciao:proposal-intent:'
const MUTATION_LOCK = 'ciao:proposal-mutation'
type BatchScope = { targets: string[]; workspaces: string[]; workspace: string; destinations?: Record<string, string> }
type SweepScope = { cutoff: string; workspaces: string[] }
type ProposalIntent = {
  version: 1 | 2; workspace: string; id: string; token: string; action: 'accept' | 'dismiss' | 'undo'
  queueState?: 'unknown' | 'unchanged' | 'replaced'
  queueReceiptId?: string
  queueReceiptConfirmed?: boolean
  decisionConfirmed?: boolean
  target?: string
  batch?: BatchScope
  sweep?: SweepScope
  operation?: 'implement' | 'undo'
  reviewed?: boolean
}

function intentKey(intent: ProposalIntent): string {
  return INTENT_FAMILY + `v${intent.version}:` + encodeURIComponent(intent.workspace) + ':' + encodeURIComponent(intent.id) + ':' + intent.token
}

function readIntents(): ProposalIntent[] {
  const intents: ProposalIntent[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (!key?.startsWith(INTENT_FAMILY)) continue
    if (!key.startsWith(INTENT_FAMILY + 'v1:') && !key.startsWith(INTENT_FAMILY + 'v2:')) throw new Error('unsupported intent version')
    const raw = localStorage.getItem(key)
    if (raw === null || raw.length > 65536 || intents.length >= 512) throw new Error('invalid intent')
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid intent')
    const item = value as Record<string, unknown>
    const allowed = ['version', 'workspace', 'id', 'token', 'action', 'queueState', 'queueReceiptId', 'queueReceiptConfirmed', 'decisionConfirmed', 'target', 'batch', 'sweep', 'operation', 'reviewed']
    if (Object.keys(item).some(key => !allowed.includes(key))
      || (item.version !== 1 && item.version !== 2) || typeof item.id !== 'string' || !item.id || item.id.length > 256
      || typeof item.workspace !== 'string' || item.workspace.length > 256
      || typeof item.token !== 'string' || !(item.version === 1 ? /^[0-9a-f]{32}$/ : /^[0-9a-f]{32}\.[0-9a-f]{32}$/).test(item.token)
      || (item.action !== 'accept' && item.action !== 'dismiss' && item.action !== 'undo')
      || (item.queueState !== undefined && !['unknown', 'unchanged', 'replaced'].includes(String(item.queueState)))
      || (item.queueReceiptId !== undefined && (typeof item.queueReceiptId !== 'string' || item.queueReceiptId.length > 256))
      || (item.queueReceiptConfirmed !== undefined && typeof item.queueReceiptConfirmed !== 'boolean')
      || (item.decisionConfirmed !== undefined && typeof item.decisionConfirmed !== 'boolean')) throw new Error('invalid intent')
    if (item.operation !== undefined && (item.version !== 2 || item.batch !== undefined || item.sweep !== undefined
      || (item.operation === 'undo' ? item.action !== 'undo' || !item.id.startsWith('__undo__:') || item.id.length <= 9
        : item.operation !== 'implement' || item.action !== 'accept'))) throw new Error('invalid intent operation')
    if (item.version === 2 && item.action === 'undo' && item.operation !== 'undo') throw new Error('invalid intent action')
    if (item.reviewed !== undefined && (item.reviewed !== true || !item.batch)) throw new Error('invalid review state')
    if (item.target !== undefined && (typeof item.target !== 'string' || !item.target || item.target.length > 256)) throw new Error('invalid intent target')
    if (item.batch !== undefined) {
      const batch = item.batch as Record<string, unknown>
      if (item.version !== 2 || !batch || typeof batch !== 'object' || Array.isArray(batch)
        || !['targets,workspace,workspaces', 'destinations,targets,workspace,workspaces'].includes(Object.keys(batch).sort().join(','))
        || typeof batch.workspace !== 'string' || batch.workspace.length > 256
        || !Array.isArray(batch.targets) || batch.targets.length < 1 || batch.targets.length > 200
        || batch.targets.some(id => typeof id !== 'string' || !/^[A-Za-z0-9_:-]{1,256}$/.test(id))
        || new Set(batch.targets).size !== batch.targets.length || !batch.targets.includes(item.id)
        || !Array.isArray(batch.workspaces) || batch.workspaces.length < 1 || batch.workspaces.length > 200
        || batch.workspaces.some(ws => typeof ws !== 'string' || ws.length > 256)
        || [...new Set(batch.workspaces)].sort().join('\0') !== batch.workspaces.join('\0')
        || !batch.workspaces.includes(item.workspace)) throw new Error('invalid batch intent')
      if (batch.destinations !== undefined) {
        const destinations = batch.destinations
        if (item.action !== 'accept' || !destinations || typeof destinations !== 'object' || Array.isArray(destinations)
          || Object.entries(destinations).some(([id, target]) => !(batch.targets as string[]).includes(id)
            || typeof target !== 'string' || !target || target.length > 256)
          || (destinations as Record<string, string>)[item.id as string] !== item.target) throw new Error('invalid batch destinations')
      }
    }
    if (item.sweep !== undefined) {
      const sweep = item.sweep as Record<string, unknown>
      if (item.version !== 2 || item.id !== '__sweep__' || item.workspace !== '' || item.action !== 'dismiss'
        || item.batch !== undefined || !sweep || typeof sweep !== 'object' || Array.isArray(sweep)
        || Object.keys(sweep).sort().join(',') !== 'cutoff,workspaces'
        || typeof sweep.cutoff !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(sweep.cutoff)
        || !Array.isArray(sweep.workspaces) || sweep.workspaces.length > 200
        || sweep.workspaces.some(ws => typeof ws !== 'string' || ws.length > 256)
        || [...new Set(sweep.workspaces)].sort().join('\0') !== sweep.workspaces.join('\0')) throw new Error('invalid sweep intent')
    }
    if (item.version === 2 && item.id === '__sweep__' && item.sweep === undefined) throw new Error('invalid sweep intent')
    const intent = item as ProposalIntent
    if (intentKey(intent) !== key) throw new Error('invalid intent key')
    intents.push(intent)
  }
  for (const intent of intents) {
    const siblings = intents.filter(other => other.token === intent.token && other !== intent)
    if (siblings.some(other => !intent.batch || !other.batch || intent.action !== other.action
      || JSON.stringify(intent.batch) !== JSON.stringify(other.batch))) throw new Error('inconsistent batch intents')
  }
  return intents
}

/** A row the operator has to settle: marked `you`, carrying no decider, or a skill row. */
export function needsYou(row: ProposalRow): boolean {
  return row.decider !== 'nightly'
}

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
  const lastBatchResults = ref<ProposalActionResult[]>([])
  const partialWarnings = ref<Record<string, { id: string; text: string; workspace: string; action: string; target?: string; effect?: ProposalAcceptRefusal['destination_effect']; queueState: string; decisionConfirmed?: boolean; queueReceiptConfirmed?: boolean; queueReceiptId?: string; reviewed?: boolean }>>({})
  const intentStorageUnavailable = ref(false)
  const intentLockError = ref(typeof navigator === 'undefined' || !navigator.locks?.request
    ? 'Web Locks are unavailable in this browser. Proposal decisions and acknowledgements are blocked.' : '')
  const intents = new Map<string, ProposalIntent>()
  function refreshIntents() {
    try {
      const found = readIntents()
      const next = new Map<string, ProposalIntent>()
      const warnings: typeof partialWarnings.value = Object.fromEntries(
        Object.entries(partialWarnings.value).filter(([id]) => !intents.has(id)),
      )
      for (const intent of found) {
        if (next.has(intent.id)) throw new Error('duplicate proposal intent')
        next.set(intent.id, intent)
        warnings[intent.id] = {
          ...partialWarnings.value[intent.id],
          id: intent.id, text: intent.id === '__sweep__' ? 'Expiry sweep across all workspaces' : intent.id,
          workspace: intent.workspace, action: intent.action, queueState: intent.queueState || 'unknown',
          target: intent.target,
          queueReceiptId: intent.queueReceiptId, queueReceiptConfirmed: intent.queueReceiptConfirmed,
          decisionConfirmed: intent.decisionConfirmed,
          reviewed: intent.reviewed,
        }
      }
      intents.clear()
      for (const [id, intent] of next) intents.set(id, intent)
      partialWarnings.value = warnings
    } catch {
      intentStorageUnavailable.value = true
    }
  }
  refreshIntents()
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key.startsWith(INTENT_FAMILY)) refreshIntents()
  }
  if (typeof window !== 'undefined') {
    window.addEventListener('storage', onStorage)
    onScopeDispose(() => window.removeEventListener('storage', onStorage))
  }

  async function withMutationLock<T>(run: () => Promise<T> | T, refused: T, acknowledgement = false): Promise<T> {
    if (typeof navigator === 'undefined' || !navigator.locks?.request) {
      intentLockError.value = 'Web Locks are unavailable in this browser. Proposal decisions and acknowledgements are blocked.'
      return refused
    }
    try {
      return await navigator.locks.request(MUTATION_LOCK, { ifAvailable: true }, async lock => {
        if (!lock) {
          intentLockError.value = 'A proposal operation is in progress in another tab. Try again when it finishes.'
          return refused
        }
        intentLockError.value = ''
        refreshIntents()
        if (intentStorageUnavailable.value) return refused
        if (!acknowledgement && intents.size > 0) {
          intentLockError.value = 'An unresolved proposal operation needs inspection and server confirmation before retrying.'
          return refused
        }
        return await run()
      })
    } catch {
      intentLockError.value = 'The browser could not verify the proposal safety lock. Decisions are blocked.'
      return refused
    }
  }

  function beginIntent(id: string, action: 'accept' | 'dismiss' | 'undo', scope?: string, target?: string, reservation?: string, batch?: BatchScope, sweep?: SweepScope, operation?: 'implement' | 'undo'): boolean {
    const workspace = scope ?? rows.value.find(row => row.id === id)?.workspace
    if (workspace === undefined || workspace.length > 256 || !id || id.length > 256
        || (target !== undefined && (!target || target.length > 256)) || intentStorageUnavailable.value) return false
    try {
      const existing = readIntents()
      const globalIntent = (item: string) => item === '__sweep__' || item.startsWith('__undo__:')
      if (existing.length >= 512 || existing.some(item => item.id === id || globalIntent(item.id))
          || (globalIntent(id) && existing.length > 0)) return false
      const token = reservation ?? Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
      const intent: ProposalIntent = { version: reservation ? 2 : 1, workspace, id, action, token, ...(target ? { target } : {}), ...(batch ? { batch } : {}), ...(sweep ? { sweep } : {}), ...(operation ? { operation } : {}) }
      const key = intentKey(intent)
      const serialized = JSON.stringify(intent)
      localStorage.setItem(key, serialized)
      if (localStorage.getItem(key) !== serialized) throw new Error('intent write failed')
      intents.set(id, intent)
      return true
    } catch {
      intentStorageUnavailable.value = true
      return false
    }
  }

  function clearIntent(id: string): boolean {
    const intent = intents.get(id)
    if (!intent) return true
    try {
      const key = intentKey(intent)
      localStorage.removeItem(key)
      if (localStorage.getItem(key) !== null) throw new Error('intent removal failed')
      intents.delete(id)
      return true
    } catch {
      intentStorageUnavailable.value = true
      return false
    }
  }

  function rememberPartial(id: string, payload: ProposalAcceptRefusal, action = 'accept'): boolean {
    if (payload?.queue_settled !== false) return false
    const row = rows.value.find(row => row.id === id)
    const text = row?.text || (id === '__sweep__' ? 'Expiry sweep across all workspaces'
      : id.startsWith('__undo__:') ? `Receipt undo: ${id.slice(9)}` : id)
    partialWarnings.value = { ...partialWarnings.value, [id]: {
      id, text, workspace: row?.workspace ?? intents.get(id)?.workspace ?? '', action,
      target: intents.get(id)?.target, effect: payload.destination_effect, queueState: payload.queue_state || 'unknown',
      decisionConfirmed: payload.decision_confirmed, queueReceiptConfirmed: payload.queue_receipt_confirmed,
      queueReceiptId: payload.queue_receipt_id,
    } }
    const intent = intents.get(id)
    if (intent) {
      const state = payload.queue_state
      const updated: ProposalIntent = {
        ...intent,
        ...(state === 'unchanged' || state === 'replaced' || state === 'unknown' ? { queueState: state } : {}),
        ...(payload.queue_receipt_id && payload.queue_receipt_id.length <= 256 ? { queueReceiptId: payload.queue_receipt_id } : {}),
        ...(typeof payload.queue_receipt_confirmed === 'boolean' ? { queueReceiptConfirmed: payload.queue_receipt_confirmed } : {}),
        ...(typeof payload.decision_confirmed === 'boolean' ? { decisionConfirmed: payload.decision_confirmed } : {}),
      }
      try {
        const key = intentKey(updated)
        const serialized = JSON.stringify(updated)
        localStorage.setItem(key, serialized)
        if (localStorage.getItem(key) !== serialized) throw new Error('intent update failed')
        intents.set(id, updated)
      } catch {
        intentStorageUnavailable.value = true
      }
    }
    return true
  }

  async function sealReservation(intent: ProposalIntent | undefined): Promise<boolean> {
    if (!intent || intent.version !== 2) return false
    try {
      const reply = await api.postReservation<{ sealed?: boolean }>('/api/proposals/reservations/seal', {
        id: intent.token,
        scope: intent.operation
          ? { kind: intent.operation, workspace: intent.workspace, targets: [intent.operation === 'undo' ? intent.id.slice(9) : intent.id] }
          : intent.sweep
          ? { kind: 'sweep', workspace: '', targets: [intent.sweep.cutoff], workspaces: intent.sweep.workspaces }
          : intent.batch
          ? { kind: `batch_${intent.action}`, ...intent.batch }
          : { kind: `row_${intent.action}`, workspace: intent.workspace, targets: [intent.id],
            ...(intent.target ? { destination: intent.target } : {}) },
        inspected: true,
      })
      if (reply?.sealed !== true) throw new Error('invalid seal response')
      return true
    } catch {
      intentLockError.value = 'Server completion could not be confirmed. Do not retry; inspect the destination, queue and receipts.'
      return false
    }
  }

  async function sealIntent(id: string): Promise<boolean> {
    return await sealReservation(intents.get(id)) && clearIntent(id)
  }

  function reviewBatchIntent(id: string): boolean {
    const intent = intents.get(id)
    if (!intent?.batch) return false
    try {
      const updated: ProposalIntent = { ...intent, reviewed: true }
      const key = intentKey(updated)
      const serialized = JSON.stringify(updated)
      localStorage.setItem(key, serialized)
      if (localStorage.getItem(key) !== serialized) throw new Error('review write failed')
      intents.set(id, updated)
      if (partialWarnings.value[id]) partialWarnings.value[id].reviewed = true
      return true
    } catch {
      intentStorageUnavailable.value = true
      return false
    }
  }

  async function finishReviewedBatch(intent: ProposalIntent): Promise<void> {
    const siblings = [...intents.values()].filter(item => item.token === intent.token)
    if (!siblings.length || siblings.some(item => !item.reviewed)) return
    if (!await sealReservation(intent)) return
    let cleared = true
    for (const sibling of siblings) if (!clearIntent(sibling.id)) cleared = false
    if (!cleared) return
    const next = { ...partialWarnings.value }
    for (const sibling of siblings) delete next[sibling.id]
    partialWarnings.value = next
  }

  async function abandonReservation(id: string, scope: object): Promise<void> {
    try {
      await api.postReservation('/api/proposals/reservations/seal', { id, scope, inspected: false })
    } catch {
      // Nothing was dispatched; an unsealed reservation expires server-side.
    }
  }

  async function acknowledgePartialOwned(id: string) {
    const intent = intents.get(id)
    if (intent?.version === 1) {
      intentLockError.value = 'This legacy warning cannot verify whether server work has finished. Do not retry; stop the engine and inspect the destination, queue and receipts before resolving browser storage manually.'
      return
    }
    if (intent?.batch) {
      if (reviewBatchIntent(id)) await finishReviewedBatch(intent)
      return
    }
    if (intent && !await sealIntent(id)) return
    const next = { ...partialWarnings.value }
    delete next[id]
    partialWarnings.value = next
  }

  function canAct(id: string): boolean {
    return !intentStorageUnavailable.value && !partialWarnings.value[id] && !intents.has(id)
      && !partialWarnings.value.__sweep__ && !intents.has('__sweep__')
      && ![...intents.keys()].some(key => key.startsWith('__undo__:'))
  }

  function hasStoredIntent(id: string): boolean {
    return intents.has(id)
  }

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

  // -- Row previews ----------------------------------------------------------
  //
  // Every queue row states what accepting it does — create a note, add to one,
  // merge into one — so the preview is fetched for the rows on screen without
  // waiting for a click. At most PREFETCH_CONCURRENCY run at once: a queue of
  // forty rows must not open forty requests that each read a destination file.
  // An id is tried once per session; a failure stays on the row as an error
  // with its own retry (`loadPreview`) rather than being re-requested on
  // every render.
  const PREFETCH_CONCURRENCY = 3
  const prefetchQueue: string[] = []
  const prefetchTried = new Set<string>()
  let prefetchActive = 0

  /** Re-prefetch dropped previews for the rows that are still queued. */
  function refetchPreviews(ids: string[]) {
    if (!ids.length) return
    const queued = new Set(rows.value.map(r => r.id))
    const still = ids.filter(id => queued.has(id))
    if (still.length) void prefetchPreviews(still)
  }

  function prefetchPreviews(ids: string[], limit = PREFETCH_CONCURRENCY): Promise<void> {
    for (const id of ids) {
      if (prefetchTried.has(id) || previews.value[id] || previewLoading.value.has(id)) continue
      prefetchTried.add(id)
      prefetchQueue.push(id)
    }
    const workers: Promise<void>[] = []
    while (prefetchActive < limit && prefetchQueue.length) {
      prefetchActive += 1
      workers.push((async () => {
        try {
          let id = prefetchQueue.shift()
          while (id !== undefined) {
            // Loaded meanwhile (a card opened it): nothing to fetch.
            if (!previews.value[id] && !previewLoading.value.has(id)) await loadPreview(id)
            id = prefetchQueue.shift()
          }
        } finally {
          prefetchActive -= 1
        }
      })())
    }
    return Promise.all(workers).then(() => undefined)
  }

  /** Previews of other rows that write the same file as `ids` were computed
   * against the destination those accepts just changed. Their diff and their
   * pinned revision are both out of date, so drop them (and let the prefetch
   * try them again) before the next accept hands a stale revision back and is
   * refused as a conflict. Returns the dropped ids for the caller to refetch.
   * Conflicted rows keep theirs: they are re-previewed explicitly. */
  function dropSiblingPreviews(ids: string[]): string[] {
    const acted = new Set(ids)
    const destinations = new Set(
      ids.map(id => previews.value[id]?.destination).filter((d): d is string => Boolean(d)),
    )
    if (!destinations.size) return []
    const dropped: string[] = []
    for (const [id, preview] of Object.entries(previews.value)) {
      if (acted.has(id) || conflictIds.value.has(id)) continue
      if (!destinations.has(preview.destination)) continue
      dropPreview(id)
      dropped.push(id)
    }
    return dropped
  }

  function dropPreview(id: string) {
    // A dropped preview may be wanted again (a sibling accept moved its
    // destination, or an undo re-queued the row), so the prefetch may retry it.
    prefetchTried.delete(id)
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

  /** Receipt ids are content-derived (`mrcpt_<sha>`), so the same id can name
   * different operations in different workspaces. Every cache slot below is
   * keyed by workspace + id, never by id alone. */
  function receiptKey(id: string, workspace = ''): string {
    return `${workspace}${id}`
  }

  function isReceiptLoading(id: string, workspace = ''): boolean {
    return receiptLoading.value.has(receiptKey(id, workspace))
  }

  async function loadReceipt(id: string, workspace = ''): Promise<MemoryReceiptDetail | null> {
    const key = receiptKey(id, workspace)
    if (receipts.value[key]) return receipts.value[key]
    const busySet = new Set(receiptLoading.value)
    busySet.add(key)
    receiptLoading.value = busySet
    try {
      const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
      const detail = await api.get<MemoryReceiptDetail>(`/api/memory/receipts/${id}${query}`)
      if (detail) receipts.value = { ...receipts.value, [key]: detail }
      return detail ?? null
    } catch (e) {
      receiptErrors.value = {
        ...receiptErrors.value,
        [key]: e instanceof Error ? e.message : 'Could not read the change',
      }
      return null
    } finally {
      const next = new Set(receiptLoading.value)
      next.delete(key)
      receiptLoading.value = next
    }
  }

  /** Reverse one receipt. The server refuses (409) when the destination moved
   * since the operation, because restoring the before image would delete an
   * unrelated later fact; that refusal is surfaced verbatim rather than
   * retried. */
  async function undoReceipt(id: string, workspace = ''): Promise<{ ok: boolean; error?: string }> {
    const intentId = `__undo__:${id}`
    if (Object.keys(partialWarnings.value).length || !canAct(intentId)) {
      return { ok: false, error: 'Inspect settlement warnings and browser storage before retrying.' }
    }
    let reservation: string
    let actualWorkspace: string
    try {
      const reply = await api.postReservation<{ id?: unknown; scope?: unknown }>('/api/proposals/reservations', {
        kind: 'undo', workspace, targets: [id],
      })
      const scope = reply?.scope as { kind?: unknown; workspace?: unknown; targets?: unknown } | undefined
      if (typeof reply?.id !== 'string' || !/^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(reply.id)
        || !scope || scope.kind !== 'undo' || typeof scope.workspace !== 'string' || !scope.workspace
        || scope.workspace.length > 256 || (workspace && scope.workspace !== workspace)
        || !Array.isArray(scope.targets) || scope.targets.length !== 1 || scope.targets[0] !== id) throw new Error('invalid undo reservation')
      reservation = reply.id
      actualWorkspace = scope.workspace
    } catch {
      return { ok: false, error: 'Could not reserve the undo; nothing was sent.' }
    }
    if (!beginIntent(intentId, 'undo', actualWorkspace, undefined, reservation, undefined, undefined, 'undo')) {
      await abandonReservation(reservation, { kind: 'undo', workspace: actualWorkspace, targets: [id] })
      return { ok: false, error: 'Cannot safely save the undo intent; inspect browser storage.' }
    }
    setBusy(id, true)
    const key = receiptKey(id, workspace)
    const next = { ...receiptErrors.value }
    delete next[key]
    receiptErrors.value = next
    try {
      const query = `?workspace=${encodeURIComponent(actualWorkspace)}`
      const reply = await api.post<{ ok?: boolean; id?: string }>(`/api/memory/receipts/${id}/undo${query}`,
        undefined, { headers: { 'X-Ciao-Proposal-Reservation': reservation } })
      if (reply?.ok !== true || reply.id !== id || !await sealIntent(intentId)) {
        rememberPartial(intentId, { queue_settled: false }, 'undo')
        return { ok: false, error: 'The undo outcome is unconfirmed; inspect before retrying.' }
      }
      // The change is reversed, so the cached image describes an operation that
      // no longer holds; History refetches and the row re-reads it.
      const without = { ...receipts.value }
      delete without[key]
      receipts.value = without
      invalidateHistory()
      await fetch({ force: true })
      return { ok: true }
    } catch (e) {
      const failure = e as { status?: number; payload?: { id?: string; conflict?: boolean; undoable?: boolean } }
      const noEffect = failure?.payload?.id === id &&
        ((failure.status === 409 && failure.payload.conflict === true) ||
         (failure.status === 400 && failure.payload.undoable === false))
      if (!noEffect || !await sealIntent(intentId)) rememberPartial(intentId, { queue_settled: false }, 'undo')
      const msg = e instanceof Error ? e.message : 'Undo failed'
      receiptErrors.value = { ...receiptErrors.value, [key]: msg }
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
  // Separate from `kindFilter`, which History filtering shares: a `needs-you`
  // value there would hide every History row.
  const decideFilter = ref<'all' | 'needs-you'>('all')
  const search = ref('')
  const selected = ref<Set<string>>(new Set())

  /** The row a link from another surface asked the queue to show, or ''.
   *
   * The review panel's "Open the proposal" link names a queue row rather than
   * rendering its own copy of the card, and that row can be hidden by whatever
   * filter the operator left on the queue. Clearing those filters and naming the
   * row here is what makes the link land on the proposal rather than on a list
   * the row is not in. A one-shot id: the panel clears it once it has scrolled
   * and focused, so coming back to the queue later does not re-scroll to a row
   * somebody looked at days ago. */
  const revealedRowId = ref('')

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
  function visibleRows(workspace: string, withDecide = true): ProposalRow[] {
    const needle = search.value.trim().toLowerCase()
    return scopedRows(workspace).filter(r =>
      (kindFilter.value === 'all' || r.kind === kindFilter.value)
      && (!withDecide || decideFilter.value === 'all' || needsYou(r))
      && (!needle
        || (r.text ?? '').toLowerCase().includes(needle)
        || (r.path ?? '').toLowerCase().includes(needle)),
    )
  }

  /** Rows in scope the operator has to settle: `you`, no decider, and skill rows. */
  function needsYouCount(workspace: string): number {
    return scopedRows(workspace).filter(needsYou).length
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
    decideFilter.value = 'all'
    search.value = ''
    historyActionFilter.value = 'all'
    historyActorFilter.value = 'all'
  }

  /** Show one named row, clearing whatever would hide it.
   *
   * The kind filter and the search box are the two things that can hide a row
   * the user was linked to, and both are operator state worth keeping — so this
   * clears them rather than hunting for a "reveal" that respects them, and the
   * queue is a page away from wherever the link was pressed.
   */
  function revealRow(id: string) {
    if (!id) return
    kindFilter.value = 'all'
    decideFilter.value = 'all'
    search.value = ''
    revealedRowId.value = id
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
    opts?: { expectedRevision?: string; text?: string; reconcile?: boolean; reason?: string },
  ): Promise<{ ok: boolean; error?: string; conflict?: boolean; payload?: unknown; status?: number }> {
    if (!canAct(id)) {
      error.value = 'Inspect and acknowledge the settlement warning before acting again.'
      return { ok: false, error: error.value }
    }
    const row = rows.value.find(row => row.id === id)
    let target = action === 'accept' && row?.kind === 'rehome' && workspace ? workspace : undefined
    if (!row) return { ok: false, error: 'Proposal is unavailable.' }
    let reservation: string
    try {
      const rehome = action === 'accept' && row.kind === 'rehome'
      const reply = await api.postReservation<{ id?: unknown; scope?: unknown }>('/api/proposals/reservations', {
        kind: `row_${action}`, workspace: row.workspace, targets: [id], ...(rehome ? { destination: workspace } : {}),
      })
      if (typeof reply?.id !== 'string' || !/^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(reply.id)) throw new Error('invalid reservation')
      if (rehome) {
        const scope = reply.scope as { kind?: unknown; workspace?: unknown; targets?: unknown; destination?: unknown } | undefined
        if (!scope || scope.kind !== 'row_accept' || scope.workspace !== row.workspace
          || !Array.isArray(scope.targets) || scope.targets.length !== 1 || scope.targets[0] !== id
          || typeof scope.destination !== 'string' || !scope.destination || scope.destination.length > 256
          || (workspace && scope.destination !== workspace)) throw new Error('invalid re-home reservation')
        target = scope.destination
      }
      reservation = reply.id
    } catch {
      error.value = 'Could not reserve this proposal operation; nothing was sent.'
      return { ok: false, error: error.value }
    }
    if (!beginIntent(id, action, row.workspace, target, reservation)) {
      await abandonReservation(reservation, { kind: `row_${action}`, workspace: row.workspace, targets: [id],
        ...(target ? { destination: target } : {}) })
      error.value = 'Cannot safely save the proposal intent; inspect browser storage before retrying.'
      return { ok: false, error: error.value }
    }
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
      // Single-row decisions only: the batch bar never sends one.
      if (opts?.reason) body.reason = opts.reason
      const reply = await api.post<{ ok?: boolean; result?: ProposalActionResult } & Partial<ProposalActionResult>>(`/api/proposals/${id}/${action}${query}`, body, { headers: { 'X-Ciao-Proposal-Reservation': reservation } })
      const flatSkillDismissal = action === 'dismiss' && rows.value.some(row => row.id === id && row.kind === 'skill')
        && reply?.id === id && reply.action === 'dismiss' && reply.dismissed === true && !reply.error
      const settled = reply?.ok === true && reply.result?.id === id
        && (action !== 'dismiss' || reply.result.action === 'dismiss')
        && reply.result.dismissed === true && !reply.result.error
        && (reply.result.queue_settled === undefined || reply.result.queue_settled === true)
      if (!settled && !flatSkillDismissal) {
        rememberPartial(id, { queue_settled: false }, action)
        return { ok: false, error: 'The proposal outcome is unconfirmed; inspect before retrying.' }
      }
      if (!await sealIntent(id)) {
        rememberPartial(id, { queue_settled: false }, action)
        return { ok: false, error: 'The proposal settled but its local marker could not be cleared.' }
      }
      const stale = action === 'accept' ? dropSiblingPreviews([id]) : []
      await fetch({ force: true })
      refetchPreviews(stale)
      invalidateHistory()
      return { ok: true }
    } catch (e) {
      const conflict = adoptConflict(id, e)
      const payload = (e as { payload?: unknown } | null)?.payload
      if (payload && rememberPartial(id, payload as ProposalAcceptRefusal, action)) {
        return { ok: false, payload }
      }
      const deferred = Boolean((payload as { deferred?: boolean } | undefined)?.deferred)
      const status = (e as { status?: number } | null)?.status
      if ((payload as ProposalAcceptRefusal | undefined)?.id === id
          && (payload as ProposalAcceptRefusal | undefined)?.no_effect === true) {
        if (!await sealIntent(id)) rememberPartial(id, { queue_settled: false }, action)
      } else {
        rememberPartial(id, { queue_settled: false }, action)
      }
      const msg = e instanceof Error ? e.message : 'Action failed'
      // A conflict is not an action failure: the card is already re-rendering
      // with the refreshed preview, and a toast on top of it would say the
      // same thing twice in two places. Nor is a deferral: the caller renders
      // it on the row, with the entries it was weighed against and a retry, and
      // a toast would take all three away the moment they became relevant.
      if (!conflict && !deferred) error.value = msg
      return { ok: false, error: msg, conflict, payload, status }
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

  /** Accept one skill proposal into an implementation chat.
   *
   * A skill row is the one kind that cannot be accepted by writing: what it
   * asks for is a change to a `skills/<name>/SKILL.md` that already exists, so
   * accepting it means opening the chat that will do that work. The server owns
   * the association — it reads it off the record, so a double tap, a retry
   * after a dropped response and a second device all get the SAME chat back —
   * and it owns the prompt too. This panel used to mint the chat itself and
   * keep the link in localStorage, which is precisely what neither of those
   * could see.
   *
   * The lifecycle that comes back is the server's, read off the row on the
   * refresh below. Nothing here infers one: a chat that ends is not a decision.
   *
   * `created` is reported back so the caller can say what happened: `false` means
   * this accept was handed a chat that was already running, and claiming to have
   * started it then would be a second lie on top of the first tap's.
   */
  async function acceptSkill(
    id: string,
  ): Promise<{ ok: boolean; chatId?: string; created?: boolean; error?: string }> {
    if (!canAct(id)) {
      error.value = 'Inspect settlement warnings and browser storage before retrying.'
      return { ok: false, error: error.value }
    }
    const row = rows.value.find(item => item.id === id && item.kind === 'skill')
    if (!row) return { ok: false, error: 'Skill proposal is unavailable.' }
    let reservation: string
    try {
      const reply = await api.postReservation<{ id?: unknown }>('/api/proposals/reservations', {
        kind: 'implement', workspace: row.workspace, targets: [id],
      })
      if (typeof reply?.id !== 'string' || !/^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(reply.id)) throw new Error('invalid reservation')
      reservation = reply.id
    } catch {
      error.value = 'Could not reserve the skill implementation; nothing was sent.'
      return { ok: false, error: error.value }
    }
    if (!beginIntent(id, 'accept', row.workspace, undefined, reservation, undefined, undefined, 'implement')) {
      await abandonReservation(reservation, { kind: 'implement', workspace: row.workspace, targets: [id] })
      error.value = 'Cannot safely save the skill intent; inspect browser storage.'
      return { ok: false, error: error.value }
    }
    setBusy(id, true)
    error.value = ''
    try {
      const reply = await api.post<ProposalSkillAcceptResponse>(
        `/api/proposals/${id}/implement`,
        undefined, { headers: { 'X-Ciao-Proposal-Reservation': reservation } },
      )
      if (reply?.ok !== true || !reply.chat_id || !await sealIntent(id)) {
        rememberPartial(id, { queue_settled: false }, 'accept')
        return { ok: false, error: 'The improvement outcome is unconfirmed; inspect before retrying.' }
      }
      // The server row is the source of truth, so re-read rather than patching
      // the local copy: the lifecycle, the chat id and the evidence all come
      // from it, and a reload must not change the answer.
      await fetch({ force: true })
      invalidateHistory()
      return { ok: true, chatId: reply?.chat_id ?? '', created: reply?.created !== false }
    } catch (e) {
      rememberPartial(id, { queue_settled: false }, 'accept')
      const msg = e instanceof Error ? e.message : 'Could not start the improvement chat'
      error.value = msg
      return { ok: false, error: msg }
    } finally {
      setBusy(id, false)
    }
  }

  /** `workspace` names the destination for re-home rows in the selection. */
  async function batch(ids: string[], action: 'accept' | 'dismiss', workspace = '') {
    if (!ids.length || ids.length > 200 || new Set(ids).size !== ids.length) {
      error.value = 'Select between one and 200 distinct proposals.'
      return
    }
    if (ids.some(id => !canAct(id))) {
      error.value = 'Inspect and acknowledge each settlement warning before acting again.'
      return
    }
    const selected = ids.map(id => rows.value.find(row => row.id === id))
    if (selected.some(row => !row)) {
      error.value = 'A selected proposal is unavailable; refresh before retrying.'
      return
    }
    let batchScope: BatchScope = {
      targets: [...ids], workspace,
      workspaces: [...new Set(selected.map(row => row!.workspace))].sort(),
    }
    let reservation: string
    try {
      const reply = await api.postReservation<{ id?: unknown; scope?: unknown }>('/api/proposals/reservations', {
        kind: `batch_${action}`, ...batchScope,
      })
      if (typeof reply?.id !== 'string' || !/^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(reply.id)) throw new Error('invalid reservation')
      const scope = reply.scope as Record<string, unknown> | undefined
      if (!scope || scope.kind !== `batch_${action}` || scope.workspace !== workspace
        || JSON.stringify(scope.targets) !== JSON.stringify(batchScope.targets)
        || JSON.stringify(scope.workspaces) !== JSON.stringify(batchScope.workspaces)
        || Object.keys(scope).sort().join(',') !== (action === 'accept'
          ? 'destinations,kind,targets,workspace,workspaces' : 'kind,targets,workspace,workspaces')) throw new Error('invalid batch reservation')
      if (action === 'accept') {
        const destinations = scope.destinations
        const rehomes = selected.filter(row => row!.kind === 'rehome').map(row => row!.id)
        if (!destinations || typeof destinations !== 'object' || Array.isArray(destinations)
          || Object.keys(destinations).length !== rehomes.length
          || Object.entries(destinations).some(([id, target]) => !rehomes.includes(id) || typeof target !== 'string'
            || !target || target.length > 256 || (workspace && target !== workspace)
            || target === selected.find(row => row!.id === id)!.workspace)) throw new Error('invalid batch destinations')
        batchScope = { ...batchScope, destinations: destinations as Record<string, string> }
      }
      reservation = reply.id
    } catch {
      error.value = 'Could not reserve this batch; nothing was sent.'
      return
    }
    const started: string[] = []
    for (const id of ids) {
      const target = batchScope.destinations?.[id]
      if (!beginIntent(id, action, undefined, target, reservation, batchScope)) {
        await abandonReservation(reservation, { kind: `batch_${action}`, ...batchScope })
        for (const prior of started) clearIntent(prior)
        error.value = 'Cannot safely save batch intents; inspect browser storage before retrying.'
        return
      }
      started.push(id)
    }
    async function settle(results: ProposalActionResult[], confirmed: boolean) {
      const known: string[] = []
      for (const id of ids) {
        const matches = results.filter(row => row.id === id)
        const result = matches.length === 1 ? matches[0] : undefined
        if (confirmed && result && (result.queue_settled === undefined || result.queue_settled === true)
            && ((result.dismissed === true && !result.error) || result.no_effect === true)) {
          known.push(id)
          rememberPartial(id, { queue_settled: false }, action)
          reviewBatchIntent(id)
        } else {
          rememberPartial(id, result?.queue_settled === false ? result : { queue_settled: false }, action)
        }
      }
      if (known.length) {
        const intent = intents.get(known[0])
        if (intent) await finishReviewedBatch(intent)
      }
    }
    setBusyMany(ids, true)
    error.value = ''
    lastBatchSummary.value = []
    try {
      // Revisions only for rows whose preview this session actually loaded —
      // which, now the queue prefetches the preview each row shows, is
      // normally every row on screen. A row whose preview never arrived sends
      // none and keeps the unguarded behaviour rather than being blocked.
      const revisions: Record<string, string> = {}
      for (const id of ids) {
        const revision = previews.value[id]?.revision
        if (revision) revisions[id] = revision
      }
      const reply = await api.post<ProposalBatchResponse>('/api/proposals/batch', {
        action, ids, ...(workspace ? { workspace } : {}),
        ...(Object.keys(revisions).length ? { revisions } : {}),
      }, { headers: { 'X-Ciao-Proposal-Reservation': reservation } })
      lastBatchSummary.value = reply?.summary ?? []
      lastBatchResults.value = reply?.results ?? []
      await settle(lastBatchResults.value, reply?.ok === true)
      const conflicted = (reply?.results ?? []).filter(r => r.conflict).map(r => r.id)
      if (conflicted.length) conflictIds.value = new Set([...conflictIds.value, ...conflicted])
      // Previews of rows that are gone would otherwise be handed to the next
      // batch as revisions for ids the server no longer knows — but a
      // conflicted row is still queued, and dropping its preview here would
      // also clear its conflict flag (see dropPreview) and leave the next
      // bulk accept with no revision: the guarded write would silently
      // downgrade to unguarded on exactly the rows known to have moved.
      // Those rows stay, and are re-previewed below onto fresh state.
      const stale = action === 'accept' ? dropSiblingPreviews(ids.filter(id => !conflicted.includes(id))) : []
      for (const id of ids) if (!conflicted.includes(id)) dropPreview(id)
      await fetch({ force: true })
      refetchPreviews(stale)
      for (const id of conflicted) await loadPreview(id)
      invalidateHistory()
    } catch (e) {
      const payload = (e as { payload?: ProposalBatchResponse & ProposalAcceptRefusal } | null)?.payload
      const idsRefused = (payload as { ids?: unknown } | undefined)?.ids
      if (payload?.no_effect === true && Array.isArray(idsRefused)
          && idsRefused.length === ids.length && ids.every(id => idsRefused.filter(item => item === id).length === 1)) {
        const sealed = await sealReservation(intents.get(ids[0]))
        for (const id of ids) if (!sealed || !clearIntent(id)) rememberPartial(id, { queue_settled: false }, action)
      } else if (payload?.results) {
        lastBatchResults.value = payload.results
        await settle(payload.results, false)
      } else if (payload?.queue_settled === false) {
        for (const id of ids) rememberPartial(id, payload, action)
      } else {
        for (const id of ids) rememberPartial(id, { queue_settled: false }, action)
      }
      error.value = e instanceof Error ? e.message : 'Batch action failed'
    } finally {
      setBusyMany(ids, false)
    }
  }

  async function dismissOlderThan(date: string) {
    // The server sweep traverses every workspace, not just the visible one.
    if (intentStorageUnavailable.value || Object.keys(partialWarnings.value).length) {
      error.value = 'Inspect and acknowledge settlement warnings before dismissing older proposals.'
      return
    }
    let reservation: string
    let sweep: SweepScope
    try {
      const reply = await api.postReservation<{ id?: unknown; scope?: unknown }>('/api/proposals/reservations', {
        kind: 'sweep', workspace: '', targets: [date],
      })
      const scope = reply?.scope as { kind?: unknown; workspace?: unknown; targets?: unknown; workspaces?: unknown } | undefined
      if (typeof reply?.id !== 'string' || !/^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(reply.id)
        || !scope || scope.kind !== 'sweep' || scope.workspace !== ''
        || !Array.isArray(scope.targets) || scope.targets.length !== 1 || scope.targets[0] !== date
        || !Array.isArray(scope.workspaces) || scope.workspaces.length > 200
        || scope.workspaces.some(ws => typeof ws !== 'string' || ws.length > 256)
        || [...new Set(scope.workspaces)].sort().join('\0') !== scope.workspaces.join('\0')) throw new Error('invalid sweep reservation')
      reservation = reply.id
      sweep = { cutoff: date, workspaces: scope.workspaces }
    } catch {
      error.value = 'Could not reserve the sweep; nothing was sent.'
      return
    }
    if (!beginIntent('__sweep__', 'dismiss', '', undefined, reservation, undefined, sweep)) {
      await abandonReservation(reservation, { kind: 'sweep', workspace: '', targets: [date], workspaces: sweep.workspaces })
      error.value = 'Cannot safely save the sweep intent; inspect browser storage before retrying.'
      return
    }
    const key = '__dismissOlderThan__'
    setBusy(key, true)
    error.value = ''
    try {
      const reply = await api.post<ProposalDismissOlderResponse>(
        `/api/proposals/dismiss-older-than?date=${encodeURIComponent(date)}`,
        undefined, { headers: { 'X-Ciao-Proposal-Reservation': reservation } },
      )
      if (reply?.ok !== true || typeof reply.removed !== 'number' || !await sealIntent('__sweep__')) {
        rememberPartial('__sweep__', { queue_settled: false }, 'dismiss')
        error.value = 'The sweep outcome is unconfirmed; inspect all workspaces before retrying.'
        return
      }
      await fetch({ force: true })
      invalidateHistory()
    } catch (e) {
      const payload = (e as { payload?: ProposalAcceptRefusal } | null)?.payload
      rememberPartial('__sweep__', payload?.queue_settled === false ? payload : { queue_settled: false }, 'dismiss')
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
    rows, loading, loaded, busy, busyIds, isBusy, setBusy, setBusyMany, error, loadError, fetch, ensureLoaded,
    act: (...args: Parameters<typeof act>) => withMutationLock(() => act(...args), { ok: false, error: 'Proposal safety lock unavailable or busy.' }),
    acceptSkill: (...args: Parameters<typeof acceptSkill>) => withMutationLock(() => acceptSkill(...args), { ok: false, error: 'Proposal safety lock unavailable or busy.' }),
    batch: (...args: Parameters<typeof batch>) => withMutationLock(() => batch(...args), undefined),
    dismissOlderThan: (...args: Parameters<typeof dismissOlderThan>) => withMutationLock(() => dismissOlderThan(...args), undefined),
    previews, previewErrors, isPreviewLoading, loadPreview, prefetchPreviews, dropPreview, conflictIds, lastBatchSummary, lastBatchResults, partialWarnings, rememberPartial,
    acknowledgePartial: (id: string) => withMutationLock(() => acknowledgePartialOwned(id), undefined, true), canAct, hasStoredIntent, intentStorageUnavailable, intentLockError,
    receipts, receiptErrors, receiptKey, isReceiptLoading, loadReceipt,
    undoReceipt: (...args: Parameters<typeof undoReceipt>) => withMutationLock(() => undoReceipt(...args), { ok: false, error: 'Proposal safety lock unavailable or busy.' }),
    kindFilter, decideFilter, search, selected, revealedRowId, revealRow,
    scopedRows, visibleRows, kindCounts, needsYouCount, resetFilters,
    view, historyRows, historyLoading, historyLoaded, historyTruncated, historyLimit,
    historyAtMax, historyTotal, historyCanLoadMore, historyFiltersActive, historyError, historyWorkspace,
    historyActionFilter, historyActorFilter,
    fetchHistory, ensureHistoryLoaded, loadMoreHistory, invalidateHistory, scopedHistory, visibleHistory,
  }
})
