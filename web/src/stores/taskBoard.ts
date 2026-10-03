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
   * A list row never carries `body`, and `/api/tasks*` has no read-by-id route,
   * so the only honest source is a write: `create` and every `PATCH`/`complete`
   * answer with the record *as stored*, body included. The editor therefore only
   * offers the description for a task whose body it actually has, and a Save on
   * any other task omits `body` rather than sending an empty one — which would
   * erase the prose nobody was shown. Adding the read-by-id route is an API
   * change, and that is not this surface's to make.
   */
  const described = ref<TaskDetail | null>(null)
  /** Ticket for the newest in-flight list request; older responses are dropped. */
  let requestSeq = 0

  /**
   * Read the board for `workspace`, always.
   *
   * A refresh that fails keeps the rows it already had: the user is still
   * looking at a board they could act on, and swapping it for an error card
   * would throw that away. The pane reads `loadError` over non-empty rows as its
   * own "stale" state instead.
   */
  async function reload(workspace: string): Promise<void> {
    if (!workspace) return
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
   * Read the board for `workspace` unless it is already held.
   *
   * A workspace switch is the only thing that should re-read it: coming back to
   * a board you have already seen costs no round trip, and the explicit `reload`
   * (the pane's Retry) always does.
   */
  async function ensureLoaded(workspace: string): Promise<void> {
    if (!workspace) return
    if (loadedWorkspace.value === workspace) return
    await reload(workspace)
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

  /** The revision this board holds for a task: the one its last write read. */
  function revisionOf(taskId: string): string {
    const held = described.value?.id === taskId ? described.value.revision : ''
    if (held) return held
    const row = rows.value.find((entry) => entry.id === taskId && !isUnreadable(entry))
    return row && !isTaskInvalidRow(row) ? row.revision : ''
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
      // A create answers with the record as stored, body included: this is the
      // one place a description arrives for a task the board has never seen.
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
    ensureLoaded, reload, revisionOf,
    create, update, complete, remove, clearError,
  }
})