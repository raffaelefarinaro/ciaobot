import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import { isTaskInvalidRow, taskApiErrorMessage, taskDetailFrom, taskRowsFrom, toTaskListRow } from '../lib/taskBoard'
import type { TaskDetail, TaskListResponse, TaskRow, TaskStatus } from '../lib/types'

/** The fields a `PATCH /api/tasks/{id}` accepts, exactly as the route checks them. */
export interface TaskChanges {
  title?: string
  status?: TaskStatus
  project_id?: string | null
  due?: string | null
  assignee?: 'user' | 'agent'
  review_state?: 'none' | 'ready'
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
      rows.value = []
      loadedWorkspace.value = ''
      // Task slugs can collide across workspaces, so a description held for one
      // workspace is not a description of the other's task with the same id.
      described.value = null
    }
    const seq = ++requestSeq
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
   * Adopt the record a write answered with.
   *
   * Normalised through `taskDetailFrom` rather than trusted: a 200 whose body is
   * not the record is not a record to put on the board, and the editor writes
   * every one of these fields back. A record with no id adopts nothing — there is
   * no row it could honestly replace, and inventing an empty one would be a card
   * with no title sitting in a real column.
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
   */
  async function get(workspace: string, taskId: string): Promise<TaskDetail | null> {
    if (!workspace || !taskId) return null
    try {
      const data = await api.get<{ task?: unknown }>(
        `${taskUrl(taskId)}?workspace=${encodeURIComponent(workspace)}`,
      )
      const task = taskDetailFrom(data?.task)
      described.value = task
      // Only the revision is adopted onto the row. The list row is what the
      // dialog's fields were filled from, so replacing its other fields under an
      // open form would leave the form describing a record the card no longer
      // matches. The revision is the part a later write has to present, and this
      // read is the newest thing the board knows.
      const index = rows.value.findIndex(
        (row) => !isTaskInvalidRow(row) && row.id === task.id,
      )
      if (index >= 0) {
        rows.value.splice(index, 1, { ...rows.value[index]!, revision: task.revision })
      } else if (task.id) {
        rows.value.push(toTaskListRow(task))
      }
      return task
    } catch (e) {
      error.value = taskApiErrorMessage(e, 'Could not read the task')
      return null
    }
  }

  /** File a task. Returns the record as stored, or `null` with `error` set. */
  async function create(workspace: string, input: TaskCreateInput): Promise<TaskDetail | null> {
    if (!workspace || !input.title.trim()) return null
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
   */
  async function update(
    workspace: string,
    taskId: string,
    expectedRevision: string,
    changes: TaskChanges,
    body?: string,
  ): Promise<TaskDetail | null> {
    if (!workspace || !taskId) return null
    saving.value = true
    error.value = ''
    try {
      const data = await api.patch<{ task?: unknown }>(taskUrl(taskId), {
        workspace,
        expected_revision: expectedRevision,
        ...changes,
        ...(body === undefined ? {} : { body }),
      })
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
   */
  async function complete(
    workspace: string,
    taskId: string,
    expectedRevision: string,
  ): Promise<TaskDetail | null> {
    if (!workspace || !taskId) return null
    saving.value = true
    error.value = ''
    try {
      const data = await api.post<{ task?: unknown }>(`${taskUrl(taskId)}/complete`, {
        workspace,
        expected_revision: expectedRevision,
      })
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
   */
  async function remove(workspace: string, taskId: string, expectedRevision: string): Promise<boolean> {
    if (!workspace || !taskId) return false
    saving.value = true
    error.value = ''
    try {
      await api.del(taskUrl(taskId), { workspace, expected_revision: expectedRevision })
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

  /** Drop a message a dismissed dialog is done with. */
  function clearError(): void {
    error.value = ''
  }

  return {
    rows, loadedWorkspace, loading, loadError, error, saving, described,
    reload, revisionOf, get,
    create, update, complete, remove, clearError,
  }
})