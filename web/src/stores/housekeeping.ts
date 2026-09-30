import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage, errorPayload } from '../lib/errorMessage'
import { useProjectStore } from './projects'
import type {
  HousekeepingDismissResponse,
  HousekeepingResponse,
  HousekeepingRunResponse,
  OperatorAction,
  UpdateTaskActionResponse,
  UpdateTaskCoverageGap,
  UpdateTaskRow,
  UpdateTasksResponse,
} from '../lib/types'

const REFRESH_INTERVAL_MS = 60_000

/** What a start/dismiss/reopen call reports back, whichever way it went.
 *
 * `chatId` is separate from `ok` on purpose. A start can fail *after* the chat
 * exists (the 500 that carries a `chat_id`), and that is the one case where the
 * chat must be opened rather than thrown away: the next start sends the prompt
 * into it instead of minting a second one, so hiding it would strand a chat the
 * engine already made and lose the task's only attempt. */
export interface UpdateTaskOutcome {
  ok: boolean
  chatId: string
  resumed: boolean
  error: string
}

/**
 * Plain-language recovery guidance for a refused transition.
 *
 * The server refuses for reasons that need *different* things from the operator
 * — a task already done at this revision is not a retry, a task that arrived in a
 * newer engine needs an update, a missing chat manager needs a moment — so a
 * single generic "something went wrong" would send everybody to the same wrong
 * next step. The probes are deliberately coarse and every one of them falls
 * through to a safe default, because these are the server's own sentences
 * matched on the client: a reworded refusal degrades to the default rather than
 * showing a stranger the wrong remedy.
 */
function refusalCopy(action: string, error: unknown): string {
  const status = statusOf(error)
  const server = apiErrorMessage(error, '')
  if (status === 400) {
    return 'No workspace is selected, so this task has nowhere to run.'
  }
  if (status === 409) {
    if (server.includes('already completed')) {
      return 'This task is already marked done at this revision, so it cannot be ' +
        'started again. A newer revision is new work and gets its own card.'
    }
    if (server.includes('needs engine')) {
      return 'This task arrived in a later Ciaobot version. Update the engine to ' +
        'run it.'
    }
    if (server.includes('unknown update task')) {
      return 'This install no longer ships that task, so there is nothing to run.'
    }
    if (server.includes('chat manager is not running')) {
      return 'The assistant is not running yet, so no chat could be opened. Try ' +
        'again in a moment.'
    }
    if (server.includes('workspace')) {
      return 'This task needs a workspace that is not set up on this install.'
    }
    return `The engine declined to ${action} this task, so nothing was changed.`
  }
  if (status === 500) {
    return 'The assistant could not start the chat. Nothing was lost — try again.'
  }
  return 'Could not reach the engine, so nothing was changed. Try again.'
}

function statusOf(error: unknown): number | undefined {
  if (error && typeof error === 'object' && 'status' in error) {
    const status = (error as { status?: unknown }).status
    if (typeof status === 'number') return status
  }
  return undefined
}

/** One row's key: the identity a decision is recorded against, per 729-C.
 *
 * A revision bump is new work with its own record, so `(id, revision)` and not
 * `id` alone — two rows for the same task at different revisions are two
 * different cards that may both be visible at once. */
function taskKey(row: UpdateTaskRow): string {
  return `${row.id}@${row.revision}`
}

function query(workspace: string): string {
  return `?workspace=${encodeURIComponent(workspace)}`
}

/**
 * Home-screen operator-action strip, plus the "After this update" group beside
 * it. Mirrors `checkPackageStatus`'s best-effort contract: a failure leaves the
 * strip empty rather than showing an error. Refreshes on first access, on a
 * short interval, and on window focus, so a cleared action (run in chat)
 * disappears without a manual reload. `run(id)` performs one action's work and
 * re-renders from the response's fresh list.
 *
 * The update tasks are a second, separate request rather than an extra key on
 * `/api/housekeeping`, and that is the cheaper of the two shapes in the way that
 * matters: the housekeeping response is a *detector pass* whose whole contract
 * is that nothing in it is expensive (see `ciao/operator_actions.py`), while the
 * task list is a per-workspace question the strip does not even know how to ask.
 * Bolting it on would make one request's cost depend on the other's semantics,
 * and the plan's "do not make housekeeping's own detection walk a vault" is the
 * same rule read the other way round. Both are cheap; they are simply cheap for
 * different reasons.
 */
export const useHousekeepingStore = defineStore('housekeeping', () => {
  const actions = ref<OperatorAction[]>([])
  const loading = ref(false)
  const runningIds = ref<Set<string>>(new Set())
  let initialized = false

  // ── "After this update" ──────────────────────────────────────────────────
  //
  // Applicability and state are per workspace, so the rows are stamped with the
  // workspace they were computed for. A switch clears them rather than leaving
  // the previous workspace's answers on screen: a card claiming this workspace
  // has work to do because another one does is worse than a card that is briefly
  // absent.
  const updateTasks = ref<UpdateTaskRow[]>([])
  const updateTasksWorkspace = ref('')
  const updateTasksLoaded = ref(false)
  const updateTasksCoverageGap = ref<UpdateTaskCoverageGap | null>(null)
  const pendingUpdateTaskIds = ref<Set<string>>(new Set())
  const taskErrors = ref<Record<string, string>>({})

  async function refresh() {
    try {
      const data = await api.get<HousekeepingResponse>('/api/housekeeping')
      actions.value = data.actions ?? []
    } catch {
      // Best-effort: the strip just stays empty if the check fails.
    }
  }

  async function refreshUpdateTasks(workspace = ''): Promise<void> {
    const wanted = (workspace || useProjectStore().activeWorkspace || '').trim()
    if (!wanted) {
      // The route refuses a nameless question on purpose. Asking anyway and
      // rendering the refusal as "no tasks" would be a claim nobody made.
      return
    }
    if (updateTasksWorkspace.value !== wanted) {
      updateTasks.value = []
      updateTasksCoverageGap.value = null
      taskErrors.value = {}
    }
    updateTasksWorkspace.value = wanted
    try {
      const data = await api.get<UpdateTasksResponse>(`/api/update-tasks${query(wanted)}`)
      // A switch may have landed while this was in flight; the older answer is
      // about a workspace the reader is no longer in.
      if (updateTasksWorkspace.value !== wanted) return
      updateTasks.value = data.tasks ?? []
      updateTasksCoverageGap.value = data.coverage_gap ?? null
      updateTasksLoaded.value = true
    } catch {
      // Best-effort, exactly like `refresh` above, and for the same reason: this
      // is a background poll on the home screen, and a red line here would nag
      // about a condition the operator cannot act on from this page. The
      // previous rows are deliberately left in place — a failed refresh is not a
      // cleared list, and a cleared list would claim this workspace has no
      // tasks when the truth is that nobody could ask.
      //
      // Where a failed check *is* reported: Settings → Update task history, which
      // fetches for itself and owns the error, its retry and its empty state.
      updateTasksLoaded.value = true
    }
  }

  async function run(id: string): Promise<{ ok: boolean; summary: string }> {
    runningIds.value = new Set(runningIds.value).add(id)
    try {
      const data = await api.post<HousekeepingRunResponse>(`/api/housekeeping/${id}/run`)
      actions.value = data.actions ?? []
      if (!data.ok) {
        return { ok: false, summary: data.error || 'Run failed' }
      }
      return { ok: true, summary: data.summary || '' }
    } catch (e) {
      // Best-effort like refresh: re-fetch so the strip reflects whatever the
      // server actually did even if the run response itself was lost.
      await refresh()
      return { ok: false, summary: e instanceof Error ? e.message : 'Run failed' }
    } finally {
      const next = new Set(runningIds.value)
      next.delete(id)
      runningIds.value = next
    }
  }

  async function dismiss(id: string): Promise<{ ok: boolean; summary: string }> {
    try {
      const data = await api.post<HousekeepingDismissResponse>(`/api/housekeeping/${id}/dismiss`)
      actions.value = data.actions ?? []
      return { ok: !!data.ok, summary: data.summary || '' }
    } catch (e) {
      // Best-effort like refresh: re-fetch so the strip reflects whatever the
      // server actually did even if the dismiss response itself was lost.
      await refresh()
      return { ok: false, summary: e instanceof Error ? e.message : 'Dismiss failed' }
    }
  }

  // -- update-task transitions ---------------------------------------------

  /** Adopt the rows a state-changing reply carried, if it carried any.
   *
   * Absent rather than empty on purpose: the route leaves the key off when its
   * own decision landed but the follow-up listing could not be computed, and
   * writing `[]` there would tell the card the workspace has no tasks. On absent
   * the caller re-lists instead, so a lost listing is a retry and never a
   * silently cleared card. */
  function adoptRows(data: UpdateTaskActionResponse): boolean {
    if (!data.tasks) return false
    updateTasks.value = data.tasks
    updateTasksLoaded.value = true
    return true
  }

  function setTaskError(key: string, message: string): void {
    taskErrors.value = { ...taskErrors.value, [key]: message }
  }

  function clearTaskError(key: string): void {
    if (!(key in taskErrors.value)) return
    const next = { ...taskErrors.value }
    delete next[key]
    taskErrors.value = next
  }

  function markPending(key: string, pending: boolean): void {
    const next = new Set(pendingUpdateTaskIds.value)
    if (pending) next.add(key)
    else next.delete(key)
    pendingUpdateTaskIds.value = next
  }

  /** POST one transition. Shared by start/dismiss/reopen, which differ only in
   *  the path and the verb the refusal copy uses. */
  async function transition(
    row: UpdateTaskRow,
    action: 'start' | 'dismiss' | 'reopen',
  ): Promise<UpdateTaskOutcome> {
    const key = taskKey(row)
    const workspace = updateTasksWorkspace.value
    if (!workspace) {
      const error = 'No workspace is selected, so this task has nowhere to run.'
      setTaskError(key, error)
      return { ok: false, chatId: '', resumed: false, error }
    }
    markPending(key, true)
    clearTaskError(key)
    try {
      const data = await api.post<UpdateTaskActionResponse>(
        `/api/update-tasks/${encodeURIComponent(row.id)}/${action}${query(workspace)}`,
        action === 'dismiss' ? { reason: '' } : undefined,
      )
      // A switch may have landed while this was in flight: this reply — and any
      // row it carried — is about a workspace the reader is no longer in, and
      // re-listing would put that workspace back in front of them. Guarded
      // exactly as `refreshUpdateTasks` guards its own answer. The outcome is
      // still reported either way, because it happened: the chat really was
      // opened, or the refusal really did land.
      if (updateTasksWorkspace.value === workspace && !adoptRows(data)) {
        await refreshUpdateTasks(workspace)
      }
      if (!data.ok) {
        const error = data.error || `The engine declined to ${action} this task.`
        setTaskError(key, error)
        return { ok: false, chatId: '', resumed: false, error }
      }
      const chatId = data.chat_id || ''
      if (action === 'start' && !chatId) {
        // A start that reports success without a chat opened nothing. Say so
        // rather than clearing the card as though the task were under way.
        const error = 'The engine did not open a chat for this task.'
        setTaskError(key, error)
        return { ok: false, chatId: '', resumed: false, error }
      }
      return { ok: true, chatId, resumed: !!data.resumed, error: '' }
    } catch (e) {
      // A refusal or a lost response: the card stays exactly as it was, with the
      // reason attached. Never a clear, because a clear would read as "handled".
      // The `chat_id` on a failed start is the exception the server goes out of
      // its way to send — the chat exists and the next start retries into it.
      const payloadChatId = String(errorPayload(e)?.chat_id || '')
      const error = payloadChatId
        ? 'The chat was opened but the task could not be sent into it. Opening it ' +
          'now, and trying again, reuses that same chat.'
        : refusalCopy(action, e)
      setTaskError(key, error)
      return {
        ok: false,
        chatId: payloadChatId,
        resumed: false,
        error,
      }
    } finally {
      markPending(key, false)
    }
  }

  function startUpdateTask(row: UpdateTaskRow): Promise<UpdateTaskOutcome> {
    return transition(row, 'start')
  }

  function dismissUpdateTask(row: UpdateTaskRow): Promise<UpdateTaskOutcome> {
    return transition(row, 'dismiss')
  }

  function reopenUpdateTask(row: UpdateTaskRow): Promise<UpdateTaskOutcome> {
    return transition(row, 'reopen')
  }

  /** Re-read a task's applicability and the record that goes with it.
   *
   * The server decides how fresh an answer may be (a named window, per
   * `update_tasks.APPLICABILITY_TTL_S`) and the state file is read on every call
   * regardless, so this re-reads the decision immediately and the detector
   * answer as soon as its window allows. The card shows the check time, so
   * "nothing changed" is visible rather than guessed at. */
  async function recheckUpdateTask(): Promise<void> {
    await refreshUpdateTasks()
  }

  function init() {
    if (initialized) return
    initialized = true
    void refresh()
    void refreshUpdateTasks()
    if (typeof window !== 'undefined') {
      window.addEventListener('focus', () => {
        void refresh()
        void refreshUpdateTasks()
      })
      window.setInterval(() => {
        void refresh()
        void refreshUpdateTasks()
      }, REFRESH_INTERVAL_MS)
    }
  }

  return {
    actions,
    loading,
    runningIds,
    refresh,
    run,
    dismiss,
    updateTasks,
    updateTasksWorkspace,
    updateTasksLoaded,
    updateTasksCoverageGap,
    pendingUpdateTaskIds,
    taskErrors,
    refreshUpdateTasks,
    startUpdateTask,
    dismissUpdateTask,
    reopenUpdateTask,
    recheckUpdateTask,
    init,
  }
})
