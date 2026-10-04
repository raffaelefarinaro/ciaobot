import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage } from '../lib/errorMessage'

/**
 * The batch-run half of the import journey (#1041, C8).
 *
 * Discovery, selection and the consent preview belong to the panel
 * (`ImportSources.vue`) and have no durable state of their own: a listing is
 * metadata and a selection is client-side. A **batch** is durable — C6 files it,
 * C7's runner moves it, and it survives a reload, a second device and the next
 * day — so it is the one import thing that needs a store. This is that store and
 * nothing more: no extraction policy, no provider knowledge, no second copy of
 * the discovery rules.
 *
 * **The guards are the same ones `webhooks` and `taskBoard` carry**, and for the
 * same reason: a batch id, a status and a set of provenance rows all name one
 * workspace's private store, and the `1`–`9` shortcuts can move the pane to
 * another workspace while any of these requests is open. A late answer is
 * therefore dropped rather than drawn under a workspace that has never heard of
 * it, and a workspace *switch* clears the rows first so the previous
 * workspace's ids are never on screen under the new name.
 *
 * **Nothing here chooses a model or a provider.** The run route resolves both
 * from configuration, and the panel is told only what it is told.
 */

export type ImportBatchStatus =
  | 'queued'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'partial'

/** A batch still holding a selection somebody may act on. Mirrors C6's rule. */
const OPEN_STATUSES: ReadonlySet<string> = new Set(['queued', 'running'])

/** One selected conversation inside a batch, with the digest the run read. */
export interface ImportBatchSource {
  provider: string
  source_id: string
  content_digest: string
  status: string
}

/** C6's `BatchProgress`, in the numbers a progress surface renders. */
export interface ImportBatchProgress {
  total_sources: number
  completed_sources: number
  proposals_filed: number
  skipped: number
  current_source_id: string
}

/**
 * One filed bullet's evidence: which external conversation and which message
 * inside it the fact cites, and whether it survived review.
 */
export interface ImportProvenance {
  provider: string
  source_id: string
  anchor: string
  destination: string
  accepted: boolean
  note: string
}

export interface ImportBatch {
  batch_id: string
  workspace: string
  destination: string
  status: ImportBatchStatus
  extraction_revision: number
  sources: ImportBatchSource[]
  progress: ImportBatchProgress
  provenance: ImportProvenance[]
  created_at: string
  updated_at: string
  error: string
}

/** One `{provider, source_id}` pair — an id, never a path. */
export interface ImportSourceRef {
  provider: string
  source_id: string
}

/** Which rows the runs list draws: everything, the open ones, or the settled ones. */
export type ImportRunFilter = 'all' | 'open' | 'settled'

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

const KNOWN_STATUSES: ReadonlySet<string> = new Set([
  'queued',
  'running',
  'done',
  'failed',
  'cancelled',
  'partial',
])

function sourceFrom(value: unknown): ImportBatchSource | null {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const sourceId = str(row.source_id)
  if (!sourceId) return null
  return {
    provider: str(row.provider),
    source_id: sourceId,
    content_digest: str(row.content_digest),
    status: str(row.status),
  }
}

function provenanceFrom(value: unknown): ImportProvenance | null {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const sourceId = str(row.source_id)
  if (!sourceId) return null
  return {
    provider: str(row.provider),
    source_id: sourceId,
    anchor: str(row.anchor),
    destination: str(row.destination),
    accepted: row.accepted === true,
    note: str(row.note),
  }
}

/**
 * A batch as this store will draw it, or `null`.
 *
 * Normalised rather than trusted, and a record with no id is nothing: there is
 * no row it could honestly replace, and inventing one would be a progress bar
 * for a batch that does not exist. A status outside C6's vocabulary is read as
 * `queued` rather than invented — the run route is the only thing that moves a
 * batch, so an unknown word here is a stale build on the other end, not a state.
 */
export function importBatchFrom(value: unknown): ImportBatch | null {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const batchId = str(row.batch_id)
  if (!batchId) return null
  const progress = (row.progress ?? {}) as Record<string, unknown>
  return {
    batch_id: batchId,
    workspace: str(row.workspace),
    destination: str(row.destination),
    status: (KNOWN_STATUSES.has(str(row.status)) ? str(row.status) : 'queued') as ImportBatchStatus,
    extraction_revision: count(row.extraction_revision),
    sources: Array.isArray(row.sources)
      ? row.sources.map(sourceFrom).filter((s): s is ImportBatchSource => s !== null)
      : [],
    progress: {
      total_sources: count(progress.total_sources),
      completed_sources: count(progress.completed_sources),
      proposals_filed: count(progress.proposals_filed),
      skipped: count(progress.skipped),
      current_source_id: str(progress.current_source_id),
    },
    provenance: Array.isArray(row.provenance)
      ? row.provenance.map(provenanceFrom).filter((p): p is ImportProvenance => p !== null)
      : [],
    created_at: str(row.created_at),
    updated_at: str(row.updated_at),
    error: str(row.error),
  }
}

function batchesUrl(workspace: string): string {
  return `/api/import/batches?workspace=${encodeURIComponent(workspace)}`
}

function batchUrl(workspace: string, batchId: string): string {
  return `/api/import/batches/${encodeURIComponent(batchId)}?workspace=${encodeURIComponent(workspace)}`
}

export const useImportStore = defineStore('import', () => {
  const batches = ref<ImportBatch[]>([])
  const loadedWorkspace = ref('')
  /**
   * C6's disclosed snapshot window, in days, as the list route reports it.
   *
   * Read from the server rather than spelled out here: the sweep prunes on that
   * constant, and a second copy in the browser is a number that can drift from
   * what the engine actually does. Zero until a list has answered, which the
   * panel reads as "the server has not said" and states nothing about.
   */
  const retentionDays = ref(0)
  const loading = ref(false)
  /** List-load failures, kept apart from `error` so a failed refresh cannot
   *  wipe the message a cancel or a run left behind. */
  const loadError = ref('')
  /** Action failures only (a create, run, cancel or remove that refused). */
  const error = ref('')
  const busyIds = ref<Set<string>>(new Set())
  /** Ticket for the newest list read; an older answer is dropped, not drawn. */
  let requestSeq = 0
  /** Ticket for the newest write answer, and for any read issued after it. */
  let writeSeq = 0

  const loaded = computed(() => loadedWorkspace.value !== '')

  function isBusy(batchId: string): boolean {
    return busyIds.value.has(batchId)
  }

  function setBusy(batchId: string, on: boolean): void {
    const next = new Set(busyIds.value)
    if (on) next.add(batchId)
    else next.delete(batchId)
    busyIds.value = next
  }

  /**
   * Whether the rows on screen are still `workspace`'s.
   *
   * Every write below is made for the workspace the panel was drawing when it
   * started, and the workspace shortcuts can move the pane while it is in
   * flight. Adopting such an answer would draw another workspace's batch id,
   * status and provenance rows under the new name, and the next press on that
   * row would present those ids to a store that has never heard of them.
   */
  function drawingWorkspace(workspace: string): boolean {
    return loadedWorkspace.value === workspace
  }

  /**
   * Read this workspace's batches, always.
   *
   * A refresh that fails over rows the panel is already drawing keeps them: the
   * user is looking at imports they can act on, and the panel labels the rows
   * stale from `loadError` instead of swapping them for an error card. A
   * workspace *switch* is the exception and is not a nuance — the rows on screen
   * are the previous workspace's, so they are dropped first.
   */
  async function reload(workspace: string): Promise<void> {
    if (!workspace) return
    if (workspace !== loadedWorkspace.value) {
      batches.value = []
      loadedWorkspace.value = ''
      retentionDays.value = 0
    }
    const seq = ++requestSeq
    writeSeq++
    loading.value = true
    loadError.value = ''
    try {
      const data = await api.get<{ batches?: unknown; retention_days?: unknown }>(
        batchesUrl(workspace),
      )
      if (seq !== requestSeq) return
      const fresh = Array.isArray(data?.batches)
        ? data.batches.map(importBatchFrom).filter((b): b is ImportBatch => b !== null)
        : []
      batches.value = mergeNewer(fresh, batches.value, seq)
      retentionDays.value = count(data?.retention_days)
      loadedWorkspace.value = workspace
    } catch (e) {
      if (seq !== requestSeq) return
      loadError.value = apiErrorMessage(e, 'Could not read this workspace’s import runs.')
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /**
   * Rows from `read`, keeping a row a write adopted while this read was open.
   *
   * A read is authoritative about *which batches exist* — one it does not list
   * was removed while it was open — but it cannot know about a write that landed
   * after it was taken. A held row the read also lists keeps its record when the
   * write is newer, so a Run landing next to a refresh does not flip the row
   * back to `queued` for as long as the stale read took to answer.
   */
  function mergeNewer(read: ImportBatch[], held: ImportBatch[], seq: number): ImportBatch[] {
    const known = new Set(read.map((row) => row.batch_id))
    const writes = new Map<string, ImportBatch>()
    if (writeSeq > seq) {
      for (const row of held) if (known.has(row.batch_id)) writes.set(row.batch_id, row)
    }
    return read.map((row) => {
      const written = writes.get(row.batch_id)
      return written && written.updated_at > row.updated_at ? written : row
    })
  }

  /**
   * Read only what has not been read for this workspace.
   *
   * The panel calls this on mount and on a workspace switch, where a list already
   * held for the same workspace is still the right one to draw. A manual refresh
   * and every write's refusal go through {@link reload}, which always reads.
   */
  async function ensureLoaded(workspace: string): Promise<void> {
    if (!workspace) return
    if (loadedWorkspace.value === workspace && !loadError.value) return
    await reload(workspace)
  }

  /**
   * Adopt the record a write answered with.
   *
   * Called only once the caller has checked {@link drawingWorkspace}. A batch the
   * list does not hold yet is appended, which keeps the route's oldest-first
   * order: a batch filed now is the newest one.
   */
  function adopt(payload: unknown): ImportBatch | null {
    const batch = importBatchFrom(payload)
    if (!batch) return null
    const index = batches.value.findIndex((row) => row.batch_id === batch.batch_id)
    if (index >= 0) batches.value.splice(index, 1, batch)
    else batches.value.push(batch)
    return batch
  }

  /**
   * File a batch over a selection (C6). Returns the stored batch, or `null`.
   *
   * Only the ids go on the wire — the route rebuilds every path from the
   * workspace's configured root, so there is no field through which a caller
   * could name a file. A refusal (a Ciaobot-own session, a second open batch, a
   * conversation a live batch already covers) leaves the list alone and puts the
   * server's own sentence in `error`.
   */
  async function file(workspace: string, sources: ImportSourceRef[]): Promise<ImportBatch | null> {
    if (!workspace || !sources.length) return null
    const seq = ++writeSeq
    error.value = ''
    try {
      const data = await api.post<{ batch?: unknown }>('/api/import/batches', {
        workspace,
        sources: sources.map((s) => ({ provider: s.provider, source_id: s.source_id })),
      })
      if (seq !== writeSeq || !drawingWorkspace(workspace)) return null
      const batch = adopt(data?.batch)
      if (!batch) {
        // A 2xx whose body is not the record. Silence here would leave the
        // confirmation open with no complaint, which reads as "nothing happened"
        // for a write that may well have landed. The next read shows whether it
        // exists.
        error.value = 'The import was filed, but the server did not answer with it. Refresh to see it.'
      }
      return batch
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not file this import.')
      return null
    }
  }

  /**
   * Start a filed batch's extraction (C7). Returns the batch as it stands.
   *
   * No model call happens in the request: the route moves the batch to `running`
   * and schedules the runner, so this answer is already the run's first state and
   * the progress that follows comes from {@link reload}, not from this promise.
   * A batch already `running` is answered with its current state rather than
   * starting a second run, and a settled batch is a 409 — both are states the
   * panel can draw, so the record is adopted whenever there is one.
   */
  async function start(workspace: string, batchId: string): Promise<ImportBatch | null> {
    if (!workspace || !batchId) return null
    const seq = ++writeSeq
    setBusy(batchId, true)
    error.value = ''
    try {
      const data = await api.post<{ batch?: unknown }>(
        `/api/import/batches/${encodeURIComponent(batchId)}/run`,
        { workspace },
      )
      if (seq !== writeSeq || !drawingWorkspace(workspace)) return null
      return adopt(data?.batch)
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not start this import.')
      return null
    } finally {
      setBusy(batchId, false)
    }
  }

  /**
   * Stop a batch, keeping what it already filed.
   *
   * Idempotent, so a second press is answered with the same record rather than
   * refused. Progress and provenance stay, and input already sent to the
   * provider cannot be unsent — the panel says so beside the button, because the
   * store cannot.
   */
  async function cancel(workspace: string, batchId: string): Promise<ImportBatch | null> {
    if (!workspace || !batchId) return null
    const seq = ++writeSeq
    setBusy(batchId, true)
    error.value = ''
    try {
      const data = await api.post<{ batch?: unknown }>(
        `/api/import/batches/${encodeURIComponent(batchId)}/cancel`,
        { workspace },
      )
      if (seq !== writeSeq || !drawingWorkspace(workspace)) return null
      return adopt(data?.batch)
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not cancel this import.')
      return null
    } finally {
      setBusy(batchId, false)
    }
  }

  /**
   * Drop a batch record. Nothing else moves: the filed proposals stay queued and
   * an accepted fact stays in the vault, which is the existing review/undo path.
   *
   * `false` also comes back from a workspace switch made while the DELETE was in
   * flight — see {@link drawingWorkspace}.
   */
  async function forget(workspace: string, batchId: string): Promise<boolean> {
    if (!workspace || !batchId) return false
    const seq = ++writeSeq
    setBusy(batchId, true)
    error.value = ''
    try {
      await api.del(batchUrl(workspace, batchId))
      if (seq !== writeSeq || !drawingWorkspace(workspace)) return false
      batches.value = batches.value.filter((row) => row.batch_id !== batchId)
      return true
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not remove this import.')
      return false
    } finally {
      setBusy(batchId, false)
    }
  }

  /** Drop a message a settled row is done with. */
  function clearError(): void {
    error.value = ''
  }

  /** Whether a batch is still holding a selection somebody may act on. */
  function isOpen(batch: ImportBatch): boolean {
    return OPEN_STATUSES.has(batch.status)
  }

  return {
    batches, loadedWorkspace, loaded, retentionDays,
    loading, loadError, error,
    reload, ensureLoaded, drawingWorkspace, isOpen, isBusy,
    file, start, cancel, forget, clearError,
  }
})