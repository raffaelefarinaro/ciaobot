import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage } from '../lib/errorMessage'
import { webhookTriggerFrom } from '../lib/webhooks'
import type { WebhookCreateResponse, WebhookMode, WebhookTrigger, WebhookTriggerResponse } from '../lib/types'

/**
 * Fields accepted by `POST /api/webhooks`.
 *
 * `mode`, `project_id` and the input policy are **create-only**: `PATCH` reads
 * only `name`, `instructions` and `enabled`, so nothing here is editable later.
 * A `project_id` of null is the workspace's General project, which is a real
 * destination and not "unset".
 */
export interface WebhookCreateInput {
  name: string
  workspace: string
  instructions?: string
  project_id?: string | null
  mode?: WebhookMode
}

/** The fields a `PATCH /api/webhooks/{id}` accepts, exactly as the route checks them. */
export interface WebhookChanges {
  name?: string
  instructions?: string
  enabled?: boolean
}

function triggersUrl(workspace: string): string {
  return `/api/webhooks?workspace=${encodeURIComponent(workspace)}`
}

function triggerUrl(triggerId: string): string {
  return `/api/webhooks/${encodeURIComponent(triggerId)}`
}

/**
 * A trigger's own mode is never a question this store answers by guessing, so
 * the create input is normalised once here: an absent `mode` means the route's
 * default (`auto`) and is left off the wire rather than sent as an explicit
 * `auto`, which would make a later default change indistinguishable from a
 * stored choice.
 */
function createBody(input: WebhookCreateInput): Record<string, unknown> {
  return {
    name: input.name.trim(),
    workspace: input.workspace,
    instructions: input.instructions?.trim() ?? '',
    ...(input.project_id ? { project_id: input.project_id } : {}),
    ...(input.mode ? { mode: input.mode } : {}),
  }
}

/**
 * One workspace's webhook triggers.
 *
 * Shaped like `taskBoard`: a per-workspace `loadedWorkspace`, a load state, and
 * a `loadError` kept apart from the action `error` so a failed refresh cannot
 * wipe an unread write's message.
 *
 * **Every write presents the revision it read.** The store never guesses one —
 * `expected_revision` is a required field on the PATCH, the rotate body and the
 * DELETE query — so a section drawn from an older read is told so by the server's
 * 409 and keeps its rows, rather than overwriting what is on disk now.
 *
 * **The one-time secret is not held here.** `create` and `rotate` hand it back
 * to the caller and nothing else in this store ever sees it: there is no field
 * for it, so no later render, a list refresh or a log can echo a credential that
 * the engine only ever sends once. The dialog that shows it owns it and drops it
 * on close.
 *
 * **A ticket never costs a secret.** The tickets below decide which *record* may
 * be merged into the rows on screen; a credential that exists nowhere else is
 * handed over however late its answer arrived. See {@link create}.
 */
export const useWebhookStore = defineStore('webhooks', () => {
  const triggers = ref<WebhookTrigger[]>([])
  const loadedWorkspace = ref('')
  const loading = ref(false)
  const loadError = ref('')
  const error = ref('')
  const saving = ref(false)
  /** Ticket for the newest list read; a late answer is dropped rather than drawn. */
  let requestSeq = 0
  /**
   * Ticket for the newest write answer, and for any list read issued after it.
   *
   * Separate from `requestSeq` on purpose: a list read and a write are both
   * things this store is told, and neither may cancel the other. So `reload`
   * takes a ticket here too — a whole-list re-read is newer than a write that
   * started before it — and so does every write (its own answer is the newest
   * record this list holds). Either way the late answer stops being merged rather
   * than putting a stale revision back on a row the newer one replaced.
   */
  let writeSeq = 0

  /** Whether this workspace's list has been read at least once, successfully. */
  const loaded = computed(() => loadedWorkspace.value !== '')

  /**
   * Read the list for `workspace`, always.
   *
   * A refresh that fails over rows the section is already drawing keeps those
   * rows: the user is still looking at triggers they could act on, and swapping
   * them for an error card would throw that away. The section reads `loadError`
   * over non-empty rows as its own "stale" state instead.
   *
   * A workspace *switch* is the exception and is not a nuance: the rows on
   * screen are the previous workspace's, and keeping them would draw another
   * workspace's trigger ids under the new name — and a write from them would
   * present those ids to it. So a switch drops them first, and the new
   * workspace's own first-load and failed states are what the section shows while
   * the read is in flight.
   *
   * A write that lands while the read is open does not lose to it. Revisions are
   * monotonic per record, so the row with the higher revision is the newer fact
   * and the read keeps it — otherwise pressing Enable next to a refresh would
   * flip the row back for as long as the stale read took to answer.
   */
  async function reload(workspace: string): Promise<void> {
    if (!workspace) return
    if (workspace !== loadedWorkspace.value) {
      triggers.value = []
      loadedWorkspace.value = ''
    }
    const seq = ++requestSeq
    writeSeq++
    loading.value = true
    loadError.value = ''
    try {
      const data = await api.get<WebhookTriggerResponse>(triggersUrl(workspace))
      if (seq !== requestSeq) return
      const fresh = Array.isArray(data?.triggers) ? data.triggers : []
      triggers.value = mergeNewer(fresh, triggers.value, seq)
      loadedWorkspace.value = workspace
    } catch (e) {
      if (seq !== requestSeq) return
      loadError.value = apiErrorMessage(e, 'Could not load webhook triggers.')
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /**
   * Rows from `read`, keeping a row a write adopted while this read was open.
   *
   * `read` is the server's answer as of when it was issued, and it is
   * authoritative about *which records exist* — a row it does not list was
   * deleted while the read was open, and keeping it would invent a trigger the
   * engine no longer has. What the read cannot know is a write that landed after
   * it was taken, so a held row the read also lists is kept when its revision is
   * higher: a record's `revision` only ever grows, so that one describes a write
   * that succeeded after the read and is the truth. Otherwise pressing Enable
   * next to a refresh would flip the row back for as long as the stale read took
   * to answer.
   */
  function mergeNewer(
    read: WebhookTrigger[],
    held: WebhookTrigger[],
    seq: number,
  ): WebhookTrigger[] {
    const known = new Set(read.map((row) => row.trigger_id))
    const writes = new Map<string, WebhookTrigger>()
    // `writeSeq` advanced past this read's ticket, so a write landed while it was
    // open. An older ticket means the held rows predate the read and are its
    // business alone.
    if (writeSeq > seq) {
      for (const row of held) if (known.has(row.trigger_id)) writes.set(row.trigger_id, row)
    }
    return read.map((row) => {
      const written = writes.get(row.trigger_id)
      return written && written.revision > row.revision ? written : row
    })
  }

  /**
   * Read only what has not been read for this workspace.
   *
   * The pane calls this on mount and on a workspace switch, where a list already
   * held for the same workspace is still the right one to draw. Everything else
   * — a manual refresh, and every write's refusal — goes through {@link reload},
   * which always reads: a list drawn before somebody ran the agent CLI is stale
   * the moment it is drawn.
   */
  async function ensureLoaded(workspace: string): Promise<void> {
    if (!workspace) return
    if (loadedWorkspace.value === workspace && !loadError.value) return
    await reload(workspace)
  }

  /**
   * Whether the rows on screen are still `workspace`'s.
   *
   * Every write is made for the workspace the section was drawing when it
   * started, and the `1`–`9` shortcuts can move the pane to another one while
   * the request is in flight. The answer is a record of the workspace the write
   * was *made for*; adopting it onto the workspace that replaced one would draw
   * another workspace's trigger id and revision under the new name, and a write
   * from that row would present those ids to a workspace that has never heard of
   * them. Same guard, same reason as `taskBoard`.
   *
   * The write itself did land on disk, so the *row* it answered with is what a
   * switch costs: the next reload of the right workspace shows it. A one-time
   * secret is not a row, so `create` and `rotate` still hand it back.
   */
  function drawingWorkspace(workspace: string): boolean {
    return loadedWorkspace.value === workspace
  }

  /**
   * Adopt the record a write answered with.
   *
   * Normalised through `webhookTriggerFrom` rather than trusted, and a record
   * with no id adopts nothing: there is no row it could honestly replace, and
   * inventing one would be a card with no name sitting in a real list.
   *
   * Called only once the caller has checked {@link drawingWorkspace}, so a row it
   * pushes is a row of the list the user is looking at.
   */
  function adopt(payload: unknown): WebhookTrigger | null {
    const trigger = webhookTriggerFrom(payload)
    if (!trigger) return null
    const index = triggers.value.findIndex((row) => row.trigger_id === trigger.trigger_id)
    if (index >= 0) triggers.value.splice(index, 1, trigger)
    else triggers.value.push(trigger)
    return trigger
  }

  /**
   * The revision this list holds for a trigger: the row's.
   *
   * The row, because the list is the authoritative read of what is on disk now.
   * A revision from anywhere else is older by construction, and presenting a
   * stale one is a permanent 409 with nothing to recover with.
   */
  function revisionOf(triggerId: string): number {
    return triggers.value.find((row) => row.trigger_id === triggerId)?.revision ?? 0
  }

  /**
   * File a trigger. Returns the created record with its one-time secret, or
   * `null` with `error` set.
   *
   * The create path returns 201 with `secret` at the top level, once. The
   * returned pair is what the one-time dialog draws; the record is merged onto the
   * list so the row is there without waiting for a re-read.
   *
   * **The secret is never dropped for a ticket reason.** `reload` takes a
   * `writeSeq` ticket of its own, so a list read that started while this POST was
   * in flight used to arrive second, find `seq !== writeSeq`, and take the secret
   * down with it: the trigger was created, the only copy of its credential was
   * thrown away, `error` stayed empty, and the natural next click — Create again —
   * made a duplicate. The tickets decide the *row*: whether this record may be
   * merged into the rows on screen, and nothing else. A workspace switch costs the
   * merge too and still reveals the secret, because the alternative is a
   * credential that exists nowhere the user can read it. See
   * {@link drawingWorkspace}.
   *
   * `null` is then a refusal, or a 2xx this store cannot use: no record to draw,
   * or no secret at all. Both put a sentence in `error` whatever the tickets say,
   * because the write may well have landed and a silent no-op reads as "nothing
   * happened" for it.
   */
  async function create(input: WebhookCreateInput): Promise<WebhookCreateResponse | null> {
    if (!input.workspace || !input.name.trim()) return null
    const workspace = input.workspace
    // This write's answer is about to be the newest record this list holds.
    const seq = ++writeSeq
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<WebhookCreateResponse>('/api/webhooks', createBody(input))
      const trigger = webhookTriggerFrom(data?.trigger)
      const secret = String(data?.secret ?? '')
      if (!secret) {
        // Asked for once and not given. An empty string is a missing answer, not
        // an empty credential, and it must not be handed back as one: the caller
        // would show a dialog with nothing in it and the user would read that as
        // the whole answer. The only way back from here is a rotation.
        error.value = 'The trigger was created, but the server did not answer with a secret. Rotate the trigger to get one.'
        return null
      }
      if (!trigger) {
        // A 2xx whose body is not the record. Silence here would leave the form
        // open with no complaint, which reads as "nothing happened" for a write
        // that may well have landed. The next reload shows whether it exists.
        error.value = 'The trigger was created, but the server did not answer with it. Reload to see it.'
        return null
      }
      // Only the row is gated. A read or another write that started after this
      // one may already hold a newer record for this list, and merging here would
      // put an older revision back on the screen; the secret is not on screen
      // anywhere else, so it is handed over regardless.
      if (seq === writeSeq && drawingWorkspace(workspace)) adopt(trigger)
      return { trigger, secret }
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not create the webhook trigger.')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Edit one trigger at the revision the caller read.
   *
   * Only `name`, `instructions` and `enabled` go on the wire — `mode`,
   * `project_id` and the input policy are create-only, and a body carrying them
   * is not a wider edit, it is a request the route ignores.
   *
   * `null` comes back from a refusal *and* from a workspace switch made while
   * the PATCH was in flight; the second drops the answer instead of adopting it
   * onto the list that replaced this one. See {@link drawingWorkspace}.
   */
  async function update(
    workspace: string,
    triggerId: string,
    expectedRevision: number,
    changes: WebhookChanges,
  ): Promise<WebhookTrigger | null> {
    if (!workspace || !triggerId) return null
    // This write's answer is about to be the newest record this list holds.
    const seq = ++writeSeq
    saving.value = true
    error.value = ''
    try {
      const data = await api.patch<{ trigger?: unknown }>(triggerUrl(triggerId), {
        expected_revision: expectedRevision,
        ...changes,
      })
      if (seq !== writeSeq || !drawingWorkspace(workspace)) return null
      return adopt(data?.trigger)
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not update the webhook trigger.')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Replace a trigger's secret. Returns the record with its new one-time secret,
   * or `null` with `error` set.
   *
   * The previous secret stops working the moment this answers, which is why the
   * dialog the caller opens has to say so: a sender still holding the old value
   * gets the receiver's single 401 answer, and nothing in this store can tell it
   * which of the two credentials it has.
   *
   * **The new secret is never dropped for a ticket reason**, for the same reason
   * {@link create} keeps its own: a rotation has already killed the old credential
   * by the time this answers, so an answer lost to a `reload` that overlapped it
   * would leave the trigger with one working secret and nobody holding it. The
   * ticket gates the row merge alone; a workspace switch still reveals the secret
   * — see {@link drawingWorkspace}.
   */
  async function rotate(
    workspace: string,
    triggerId: string,
    expectedRevision: number,
  ): Promise<WebhookCreateResponse | null> {
    if (!workspace || !triggerId) return null
    const seq = ++writeSeq
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<WebhookCreateResponse>(`${triggerUrl(triggerId)}/rotate`, {
        expected_revision: expectedRevision,
      })
      const trigger = webhookTriggerFrom(data?.trigger)
      const secret = String(data?.secret ?? '')
      if (!secret) {
        // The old credential is dead whatever this answers, so a rotation with no
        // replacement cannot pass as a quiet no-op: the sender holds something the
        // receiver will refuse, and only another rotation repairs it.
        error.value = 'The secret was rotated, but the server did not answer with the new one. Rotate it again before sending anything.'
        return null
      }
      if (!trigger) {
        // The rotation may have landed, so the row itself is the only thing
        // missing: the next reload has to supply it.
        error.value = 'The secret was rotated, but the server did not answer with the trigger. Reload to see it.'
        return null
      }
      // Only the row is gated, as in `create`: an overlapping read or
      // write may already hold a newer revision for this list, and the credential
      // is on screen nowhere else.
      if (seq === writeSeq && drawingWorkspace(workspace)) adopt(trigger)
      return { trigger, secret }
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not rotate the webhook secret.')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Delete one trigger at the revision the caller read.
   *
   * `expected_revision` rides the query, which is where the route reads it, and
   * `api.del` is used for that reason rather than a body the route would ignore.
   * A refusal (a stale revision, a trigger that is already gone) leaves the rows
   * alone and puts the server's sentence in `error`.
   *
   * `false` also comes back from a workspace switch made while the DELETE was in
   * flight: a trigger id can be reused across workspaces, so a late filter would
   * hide a real row of the new list without having deleted anything. See
   * {@link drawingWorkspace}.
   */
  async function remove(workspace: string, triggerId: string, expectedRevision: number): Promise<boolean> {
    if (!workspace || !triggerId) return false
    const seq = ++writeSeq
    saving.value = true
    error.value = ''
    try {
      await api.del(
        `${triggerUrl(triggerId)}?expected_revision=${encodeURIComponent(String(expectedRevision))}`,
      )
      if (seq !== writeSeq || !drawingWorkspace(workspace)) return false
      triggers.value = triggers.value.filter((row) => row.trigger_id !== triggerId)
      return true
    } catch (e) {
      if (seq === writeSeq) error.value = apiErrorMessage(e, 'Could not delete the webhook trigger.')
      return false
    } finally {
      saving.value = false
    }
  }

  /** Drop a message a dismissed dialog is done with. */
  function clearError(): void {
    error.value = ''
  }

  return {
    triggers, loadedWorkspace, loaded, loading, loadError, error, saving,
    reload, ensureLoaded, revisionOf,
    create, update, rotate, remove, clearError,
  }
})