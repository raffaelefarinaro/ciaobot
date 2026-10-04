import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import {
  isTaskInvalidRow,
  taskApiErrorMessage,
  taskAttemptFrom,
  taskDetailFrom,
  taskRowsFrom,
  toTaskListRow,
} from '../lib/taskBoard'
import type {
  TaskAttempt,
  TaskAttemptActionResponse,
  TaskAttemptsResponse,
  TaskDelegateResponse,
  TaskDetail,
  TaskListResponse,
  TaskRow,
  TaskSendUpdateResponse,
  TaskStatus,
} from '../lib/types'

/** The fields a `PATCH /api/tasks/{id}` accepts, exactly as the route checks them. */
export interface TaskChanges {
  title?: string
  status?: TaskStatus
  project_id?: string | null
  due?: string | null
  assignee?: 'user' | 'agent'
}

export interface TaskCreateInput {
  title: string
  body?: string
  project_id?: string
  due?: string
}

function tasksUrl(workspace: string): string {
  return `/api/tasks?workspace=${encodeURIComponent(workspace)}`
}

function taskUrl(taskId: string): string {
  return `/api/tasks/${encodeURIComponent(taskId)}`
}

/**
 * One workspace's task board.
 *
 * Shaped like `entityTypes` and `vaultReview`: a per-workspace `loadedWorkspace`,
 * a load state, and a separate `loadError` so a failed refresh cannot clear an
 * unread save's message. Not a second half of `stores/tasks.ts`, which is
 * schedules — those are automations the engine runs, these are records the user
 * owns, and merging the two would put two different load contracts in one store.
 *
 * `reload` always reads. There is no "skip if already held" path: a board drawn
 * before someone ran `ciao task` or an agent edited a file is stale the moment
 * it is drawn, and the pane re-reads on mount and after a refused write.
 *
 * **Every write presents the revision it read.** The store refuses to guess one:
 * a write without `expected_revision` is a 400 from the server, and one with a
 * stale value is a 409 that writes nothing. So a board drawn from an older read
 * is told so and keeps its rows, rather than overwriting what is on disk now.
 */
export const useTaskBoardStore = defineStore('taskBoard', () => {
  const rows = ref<TaskRow[]>([])
  const loadedWorkspace = ref('')
  const loading = ref(false)
  const loadError = ref('')
  const error = ref('')
  const saving = ref(false)
  /**
   * The one task whose Markdown description this board holds.
   *
   * `GET /api/tasks/{id}` is the source: a list row carries no `body`, so the
   * read-by-id is what lets the editor show the prose it will write back. A
   * Save sends `body` only when this slot is the record for that task, so a
   * description the board never read is never overwritten with an empty one.
   */
  const described = ref<TaskDetail | null>(null)
  /** Ticket for the newest in-flight list request; older responses are dropped. */
  let requestSeq = 0
  /**
   * Ticket for the newest read-by-id. A description read is dropped once the
   * board has been told something newer.
   *
   * Separate from `requestSeq` on purpose: a list read and a description read are
   * both things this board is told, and neither may cancel the other — a dialog
   * opened while a refresh is in flight still has to get its prose. So `reload`
   * takes a ticket here too (the whole board re-read is newer than one task read
   * that started before it), and so does every write (its own answer is the
   * newest record this board holds). Either way the late read is dropped rather
   * than allowed to put a stale revision back on a row the newer answer replaced,
   * or prose older than the last write into the dialog that write updated.
   */
  let descriptionSeq = 0

  // ── Attempt history ─────────────────────────────────────────────────────
  //
  // One task's attempts at a time, read on demand, on the proposal queue's
  // History pattern (`proposals.ts`: lazily loaded, invalidated by a mutation,
  // scoped so a workspace switch refetches rather than showing the previous
  // workspace's ledger) — not a second timeline store, and not part of the list
  // read: the board draws a badge from the live attempt alone, and a workspace
  // with fifty retried tasks would pay for fifty histories nobody opened.

  /** The attempts held, live one first, for {@link attemptsTaskId} only. */
  const attempts = ref<TaskAttempt[]>([])
  /** Whose attempts {@link attempts} are; `''` when none has been read. */
  const attemptsTaskId = ref('')
  const attemptsLoading = ref(false)
  const attemptsLoaded = ref(false)
  /** History failures get their own slot: `error` belongs to a write. */
  const attemptsError = ref('')
  /** Ticket for the newest history read; older answers are dropped. */
  let attemptsSeq = 0

  /**
   * Read one task's attempts, once.
   *
   * Dropped on a workspace switch for the reason every other slot here is: task
   * ids are minted per workspace, so a history held for the workspace the user
   * left is not the history of the task with that id on the one they moved to.
   *
   * The answer also carries the task, so a history read is a fresh record: the
   * row adopts its revision, which is what keeps the next write from being a 409
   * against a board that had not noticed the move.
   */
  async function ensureAttempts(workspace: string, taskId: string): Promise<void> {
    if (!workspace || !taskId) return
    if (attemptsLoaded.value && attemptsTaskId.value === taskId) return
    if (attemptsTaskId.value && attemptsTaskId.value !== taskId) attempts.value = []
    const seq = ++attemptsSeq
    attemptsTaskId.value = taskId
    attemptsLoading.value = true
    attemptsError.value = ''
    try {
      const data = await api.get<TaskAttemptsResponse>(
        `${taskUrl(taskId)}/attempts?workspace=${encodeURIComponent(workspace)}`,
      )
      if (seq !== attemptsSeq || attemptsTaskId.value !== taskId) return
      // The rows on screen may belong to a workspace the user has since moved to;
      // adopting this record there would draw another workspace's task on it.
      if (drawingWorkspace(workspace)) {
        const task = taskDetailFrom(data?.task)
        if (task.id) {
          adopt(task)
          if (described.value?.id === task.id) described.value = task
        }
      }
      attempts.value = (Array.isArray(data?.attempts) ? data.attempts : [])
        .map((raw) => taskAttemptFrom(raw))
        .filter((attempt) => attempt.attempt_id)
      attemptsLoaded.value = true
    } catch (e) {
      if (seq !== attemptsSeq) return
      attemptsError.value = taskApiErrorMessage(e, 'Could not load the delegation history')
    } finally {
      if (seq === attemptsSeq) attemptsLoading.value = false
    }
  }

  /**
   * Mark the held history stale, after a gesture that settles or replaces one.
   *
   * A clear without a refetch: the History sheet re-reads when it is next shown,
   * the same reason the proposal queue only refetches when something is already
   * displaying its ledger.
   */
  function invalidateAttempts(): void {
    attemptsLoaded.value = false
    attemptsSeq++
    attemptsLoading.value = false
  }

  /** Drop the held history outright: a workspace switch, or a deleted task. */
  function resetAttempts(): void {
    invalidateAttempts()
    attempts.value = []
    attemptsTaskId.value = ''
    attemptsError.value = ''
  }

  /**
   * Read the board for `workspace`, always.
   *
   * A refresh that fails over rows the pane is already drawing keeps those rows:
   * the user is still looking at a board they could act on, and swapping it for
   * an error card would throw that away. The pane reads `loadError` over
   * non-empty rows as its own "stale" state instead.
   *
   * A workspace *switch* is the exception, and it is not a nuance: the rows on
   * screen are the previous workspace's, and keeping them would draw another
   * workspace's task ids and revisions under the new name — and a write from
   * them would present those ids to it. So a switch drops them first, and the
   * new workspace's own first-load and failed states are what the pane shows
   * while the read is in flight.
   */
  async function reload(workspace: string): Promise<void> {
    if (!workspace) return
    if (workspace !== loadedWorkspace.value) {
      saving.value = false
      error.value = ''
      rows.value = []
      loadedWorkspace.value = ''
      // Task slugs can collide across workspaces, so a description held for one
      // workspace is not a description of the other's task with the same id.
      described.value = null
      // The same collision applies to an attempt history, one task at a time.
      resetAttempts()
    }
    const seq = ++requestSeq
    descriptionSeq++
    loading.value = true
    loadError.value = ''
    try {
      const data = await api.get<TaskListResponse>(tasksUrl(workspace))
      if (seq !== requestSeq) return
      rows.value = taskRowsFrom(data)
      loadedWorkspace.value = workspace
    } catch (e) {
      if (seq !== requestSeq) return
      loadError.value = taskApiErrorMessage(e, 'Could not load tasks')
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /**
   * Whether the rows on screen are still `workspace`'s.
   *
   * Every write is made for the workspace the pane was drawing when it started,
   * and `1`–`9` can move the pane to another one while the request is in flight:
   * a status move or a Save is out, and the user presses a digit. The answer is a
   * record of the workspace the write was *made for*, and the rows on screen are
   * the other one's — so adopting it would draw another workspace's task id and
   * revision under the new name, and a write from that card would present those
   * ids to a workspace that has never heard of them. This is the same guard
   * {@link get} takes, for the same reason.
   *
   * The write itself did land on disk, so nothing is lost by dropping its answer:
   * the next reload of the right workspace shows it.
   */
  function drawingWorkspace(workspace: string): boolean {
    return loadedWorkspace.value === workspace
  }

  /**
   * Adopt the record a write answered with.
   *
   * Normalised through `taskDetailFrom` rather than trusted: a 200 whose body is
   * not the record is not a record to put on the board, and the editor writes
   * every one of these fields back. A record with no id adopts nothing — there is
   * no row it could honestly replace, and inventing an empty one would be a card
   * with no title sitting in a real column.
   *
   * Called only once the caller has checked {@link drawingWorkspace}, so a row it
   * pushes is a row of the board the user is looking at.
   */
  function adopt(payload: unknown): TaskDetail {
    const task = taskDetailFrom(payload)
    if (!task.id) return task
    const row = toTaskListRow(task)
    const index = rows.value.findIndex((existing) => !isUnreadable(existing) && existing.id === row.id)
    if (index >= 0) rows.value.splice(index, 1, row)
    else rows.value.push(row)
    return task
  }

  function isUnreadable(row: TaskRow): boolean {
    return isTaskInvalidRow(row)
  }

  /**
   * The revision this board holds for a task: the row's.
   *
   * The row is the answer because the list is the authoritative read of what is
   * on disk now. A description slot held from an earlier read can carry an older
   * revision, and presenting that one is a permanent 409 — the write is refused
   * every time, with nothing to recover with — so it is dropped when the two
   * disagree rather than preferred.
   */
  function revisionOf(taskId: string): string {
    const row = rows.value.find((entry) => entry.id === taskId && !isTaskInvalidRow(entry))
    const rowRevision = row && !isTaskInvalidRow(row) ? row.revision : ''
    const held = described.value?.id === taskId ? described.value : null
    if (held && rowRevision && held.revision !== rowRevision) described.value = null
    return rowRevision || held?.revision || ''
  }

  /**
   * Read one task, description included, at the workspace the caller is drawing.
   *
   * The only source of a Markdown description the list rows do not carry, so it is
   * what lets the editor show the prose before writing it back. A read that fails
   * puts the server's sentence in `error` and returns `null`: the caller keeps the
   * description out of the form, and a Save on the other fields omits `body`
   * rather than sending an empty one over prose nobody was shown.
   *
   * The answer is for the board that asked, and only while that board is still the
   * one being drawn. Two things can stop it being that. The workspace can have
   * moved on — `1`–`9` switch it while a GET is in flight, and a description read
   * for the workspace on screen a moment ago is another workspace's task, which
   * on a shared id would be indistinguishable from this one's. And a newer read of
   * the same thing can have landed: two dialogs opened back to back answer out of
   * order, and the older one must not decide what the newer is showing. Both
   * answers drop to `null` rather than an error: nothing failed, and the caller
   * that asked has since been answered by something newer.
   */
  async function get(workspace: string, taskId: string): Promise<TaskDetail | null> {
    if (!workspace || !taskId) return null
    const seq = ++descriptionSeq
    /** Whether this answer is still one the board it was asked for can use. */
    const current = () => seq === descriptionSeq && drawingWorkspace(workspace)
    try {
      const data = await api.get<{ task?: unknown }>(
        `${taskUrl(taskId)}?workspace=${encodeURIComponent(workspace)}`,
      )
      if (!current()) return null
      const task = taskDetailFrom(data?.task)
      described.value = task
      // Only the revision is adopted onto the row. The list row is what the
      // dialog's fields were filled from, so replacing its other fields under an
      // open form would leave the form describing a record the card no longer
      // matches. The revision is the part a later write has to present, and this
      // read is the newest thing the board knows.
      //
      // Only onto a row that is already there: a description read is not a source
      // of rows, and one that landed late onto a board that had just been cleared
      // would draw a task nobody listed — with an id and a revision the next write
      // would present.
      const index = rows.value.findIndex(
        (row) => !isTaskInvalidRow(row) && row.id === task.id,
      )
      if (index >= 0) {
        rows.value.splice(index, 1, { ...rows.value[index]!, revision: task.revision })
      }
      return task
    } catch (e) {
      if (!current()) return null
      error.value = taskApiErrorMessage(e, 'Could not read the task')
      return null
    }
  }

  /**
   * File a task. Returns the record as stored, or `null` with `error` set.
   *
   * `null` also means the board stopped drawing this workspace while the POST was
   * in flight: the task was filed, and its answer is dropped rather than drawn on
   * the workspace that replaced this one — see {@link drawingWorkspace}.
   */
  async function create(workspace: string, input: TaskCreateInput): Promise<TaskDetail | null> {
    if (!workspace || !input.title.trim()) return null
    // This write's answer is about to be the newest record the board holds.
    descriptionSeq++
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<{ task?: unknown }>('/api/tasks', {
        workspace,
        title: input.title.trim(),
        body: input.body ?? '',
        ...(input.project_id ? { project_id: input.project_id } : {}),
        ...(input.due ? { due: input.due } : {}),
      })
      // The board moved to another workspace while this filed. The record belongs
      // to the one that was left, and `adopt` would push it — and the description
      // with it — onto a board that never listed it.
      if (!drawingWorkspace(workspace)) return null
      const task = adopt(data?.task)
      // A create answers with the record as stored, body included, so this one
      // read saves the dialog a round trip for the task just filed.
      described.value = task
      return task
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not create the task')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Edit one task at the revision the caller read.
   *
   * `body` is sent only when there is one: the route treats a present-and-empty
   * body as "replace the description with nothing", which is right for an editor
   * that cleared it and wrong for a status move that never touched it.
   *
   * `null` comes back from a refusal *and* from a workspace switch made while the
   * PATCH was in flight; the second drops the answer instead of adopting it onto
   * the board that replaced this one. See {@link drawingWorkspace}.
   */
  async function update(
    workspace: string,
    taskId: string,
    expectedRevision: string,
    changes: TaskChanges,
    body?: string,
  ): Promise<TaskDetail | null> {
    if (!workspace || !taskId) return null
    // A description read that started before this write is behind it, and its
    // answer would put the revision it read back on the row this write moved on.
    descriptionSeq++
    saving.value = true
    error.value = ''
    try {
      const data = await api.patch<{ task?: unknown }>(taskUrl(taskId), {
        workspace,
        expected_revision: expectedRevision,
        ...changes,
        ...(body === undefined ? {} : { body }),
      })
      // The board moved to another workspace while this saved. The write landed
      // in the one that was left; its answer is not a row on the one now drawn,
      // and handing it back would let the caller draw it there.
      if (!drawingWorkspace(workspace)) return null
      const task = adopt(data?.task)
      if (task.id && described.value?.id === task.id) described.value = task
      return task
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not save the task')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Mark one task done, at the revision the caller read.
   *
   * A distinct gesture rather than `update({ status: 'done' })` because the store
   * enforces completion as the user's own act, and it refuses a task linked to a
   * live chat or attempt — refusals the board surfaces rather than swallows.
   *
   * `null` comes back from a refusal *and* from a workspace switch made while the
   * POST was in flight; see {@link drawingWorkspace}.
   */
  async function complete(
    workspace: string,
    taskId: string,
    expectedRevision: string,
  ): Promise<TaskDetail | null> {
    if (!workspace || !taskId) return null
    descriptionSeq++
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<{ task?: unknown }>(`${taskUrl(taskId)}/complete`, {
        workspace,
        expected_revision: expectedRevision,
      })
      // Approving a reviewed result releases the linkage and closes the card, so
      // whatever history was held is about an attempt that no longer holds this
      // task.
      invalidateAttempts()
      // The board moved to another workspace while this completed.
      if (!drawingWorkspace(workspace)) return null
      const task = adopt(data?.task)
      if (task.id && described.value?.id === task.id) described.value = task
      return task
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not complete the task')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Delete one task file, at the revision the caller read.
   *
   * The request carries a body because the route requires it — `api.del` takes
   * one for exactly this case. A refusal (a stale revision, a file that is
   * already gone) leaves the rows alone and puts the server's sentence in `error`.
   *
   * `false` also comes back from a workspace switch made while the DELETE was in
   * flight. The rows on screen are then the other workspace's, and a slug that
   * collides across them means a late filter would drop a real row there without
   * deleting anything. See {@link drawingWorkspace}.
   */
  async function remove(workspace: string, taskId: string, expectedRevision: string): Promise<boolean> {
    if (!workspace || !taskId) return false
    // The record this read is about is about to be gone: its answer has nowhere
    // to go, and putting it back would resurrect a file the vault has lost.
    descriptionSeq++
    saving.value = true
    error.value = ''
    try {
      await api.del(taskUrl(taskId), { workspace, expected_revision: expectedRevision })
      // The record the held history belongs to is gone.
      if (described.value?.id === taskId) described.value = null
      if (attemptsTaskId.value === taskId) resetAttempts()
      // The board moved to another workspace while this deleted. Slugs collide
      // across workspaces, so a real row of the new board can answer to the same
      // id: dropping it here would delete nothing on disk and hide a task the
      // user is looking at.
      if (!drawingWorkspace(workspace)) return false
      rows.value = rows.value.filter((row) => isUnreadable(row) || row.id !== taskId)
      if (described.value?.id === taskId) described.value = null
      return true
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not delete the task')
      return false
    } finally {
      saving.value = false
    }
  }

  /**
   * Hand one task to the agent as one ordinary chat, at the revision the board read.
   *
   * The whole of the no-escalation property lives on the server: it launches
   * through `start_stream` with the default attendance, so an approval card the
   * delegated turn raises is an ordinary Needs-you card in that chat. There is no
   * body key for prompt text either — the prompt is built from the record's own
   * fields, so a board cannot hand the agent work nobody filed.
   *
   * `created: false` in the answer is the normal outcome of a double click or a
   * race with an agent: the attempt that is already running is returned and
   * nothing was created or sent. The row is still adopted either way, because
   * either answer changed the task (or confirmed it had already changed).
   */
  async function delegate(
    workspace: string,
    taskId: string,
    expectedRevision: string,
    projectId?: string,
  ): Promise<TaskDelegateResponse['attempt'] | null> {
    if (!workspace || !taskId || !expectedRevision) return null
    // This write's answer is about to be the newest record the board holds.
    descriptionSeq++
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<TaskDelegateResponse | null>(
        `/api/tasks/${encodeURIComponent(taskId)}/delegate`,
        {
          workspace,
          expected_revision: expectedRevision,
          ...(projectId ? { project_id: projectId } : {}),
        },
      )
      // The board moved to another workspace while this delegated. The attempt
      // belongs to the one that was left; adopting it would draw this workspace's
      // chat and revision under the new name.
      if (!drawingWorkspace(workspace)) return null
      // A new attempt is the newest row of this task's history, and the attempt it
      // replaced — if any — is still in it.
      invalidateAttempts()
      if (data?.task) {
        const task = adopt(data.task)
        if (task.id && described.value?.id === task.id) described.value = task
      }
      const attempt = taskAttemptFrom(data?.attempt)
      return attempt.attempt_id ? attempt : null
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not delegate the task')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Stop, resume, retry or detach one attempt.
   *
   * No `expected_revision`, because the gesture acts on an *attempt* rather than
   * editing a record: there is no field for the board to present and no planned
   * edit that could go stale. What can conflict is the attempt's own state — a
   * `stop` on an already-stopped attempt is a 400 naming what it found — and the
   * server decides that from the attempt, not from anything this board holds.
   *
   * The task row comes back on `detach` (the linkage cleared) and on `resume`/
   * `retry` (re-linked to the new attempt), so it is adopted through the same guard
   * as every other write.
   */
  async function attemptAction(
    workspace: string,
    taskId: string,
    attemptId: string,
    action: 'stop' | 'resume' | 'retry' | 'detach',
  ): Promise<TaskAttempt | null> {
    if (!workspace || !taskId || !attemptId) return null
    descriptionSeq++
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<TaskAttemptActionResponse | null>(
        `/api/tasks/${encodeURIComponent(taskId)}/attempt/${encodeURIComponent(attemptId)}/${action}`,
        { workspace },
      )
      // The board moved to another workspace while this gesture was in flight: the
      // rows on screen are the other one's, and a late detach would filter this
      // workspace's row out of it. See {@link drawingWorkspace}.
      if (!drawingWorkspace(workspace)) return null
      // Every one of the four verbs settles an attempt, and `retry` mints another,
      // so the history a reader would draw is out of date the moment this lands.
      invalidateAttempts()
      if (data?.task) {
        const task = adopt(data.task)
        if (task.id && described.value?.id === task.id) described.value = task
      }
      const attempt = taskAttemptFrom(data?.attempt)
      return attempt.attempt_id ? attempt : null
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not act on the delegation')
      return null
    } finally {
      saving.value = false
    }
  }

  /**
   * Send the edited task into one attempt's own chat, and rebind that attempt.
   *
   * A **Send update**, and it goes through the board rather than the composer's
   * `sendMessage` for one reason the composer's send cannot serve: the server has
   * to rebind the attempt to the revision the update was made at, or
   * `changed_since_delegated` stays true and the control offers the same update
   * for ever. One message, one chat, one attempt — this is not a second
   * delegation, and nothing here mints anything.
   *
   * `message` is what the user was shown in the preview, sent as approved: it is
   * an ordinary attended message into a conversation, not an instruction the
   * board is trusted to compose. What the server checks is `expectedRevision`,
   * which is the revision the prose was read at.
   *
   * `null` comes back from a refusal *and* from a workspace switch made while
   * the POST was in flight; see {@link drawingWorkspace}. A refusal is a `null`
   * rather than a thrown error so the sheet stays open with the server's
   * sentence: the control has to survive it, or a failed send would read as a
   * delivered one.
   */
  async function sendUpdate(
    workspace: string,
    taskId: string,
    attemptId: string,
    expectedRevision: string,
    message: string,
  ): Promise<TaskAttempt | null> {
    if (!workspace || !taskId || !attemptId || !expectedRevision || !message.trim()) return null
    // This write's answer is about to be the newest record the board holds.
    const seq = ++descriptionSeq
    const current = () => seq === descriptionSeq && drawingWorkspace(workspace)
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<TaskSendUpdateResponse | null>(
        `/api/tasks/${encodeURIComponent(taskId)}/attempt/${encodeURIComponent(attemptId)}/update`,
        { workspace, expected_revision: expectedRevision, message },
      )
      // The board moved to another workspace while this was in flight: the attempt
      // and its chat belong to the one that was left, and adopting them here would
      // draw this workspace's linkage under the new name. See
      // {@link drawingWorkspace}.
      if (!current()) return null
      // The rebind changed the attempt's own record, so the history a reader would
      // draw is out of date the moment this lands.
      invalidateAttempts()
      if (data?.task) {
        const task = adopt(data.task)
        if (task.id && described.value?.id === task.id) described.value = task
      }
      const attempt = taskAttemptFrom(data?.attempt)
      return attempt.attempt_id ? attempt : null
    } catch (e) {
      if (!current()) return null
      error.value = taskApiErrorMessage(e, 'Could not send the update')
      return null
    } finally {
      if (current()) saving.value = false
    }
  }

  /** Drop a message a dismissed dialog is done with. */
  function clearError(): void {
    error.value = ''
  }

  return {
    rows, loadedWorkspace, loading, loadError, error, saving, described,
    attempts, attemptsTaskId, attemptsLoading, attemptsLoaded, attemptsError,
    reload, revisionOf, get,
    create, update, complete, remove, clearError,
    delegate, attemptAction, sendUpdate,
    ensureAttempts, invalidateAttempts, resetAttempts,
  }
})
