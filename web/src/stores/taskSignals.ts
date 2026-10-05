import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import { readableTasks, taskRowsFrom } from '../lib/taskBoard'
import type { ChatInfo, Task } from '../lib/types'

/**
 * What the rest of the app needs to know about delegated board tasks.
 *
 * The board (`stores/taskBoard.ts`) is the editor and loads only on `/tasks`.
 * This store is the app-wide read: the active workspace's readable tasks, so the
 * sidebar can count the ones waiting for review, Home can put a chat whose agent
 * asked for input among the chats that need you, and a delegated chat can link
 * back to its task. It never writes; the board does.
 *
 * Best-effort like `proposals`: a failed read keeps the last rows rather than
 * blanking every signal. `reload` is called on workspace switch and whenever a
 * `tasks_changed` event arrives on `/ws/events`.
 */
export const useTaskSignalsStore = defineStore('taskSignals', () => {
  const tasks = ref<Task[]>([])
  const loadedWorkspace = ref('')
  let inflight: Promise<void> | null = null
  let inflightWorkspace = ''
  // A reload asked for while one is reading the same workspace. The read in
  // flight may have started before the change that prompted the ask, so it is
  // not enough to join it: one more read follows. Any number of asks during a
  // read collapse into that one.
  let again = false

  async function reload(workspace: string): Promise<void> {
    if (!workspace) return
    if (inflight && inflightWorkspace === workspace) {
      again = true
      return inflight
    }
    inflightWorkspace = workspace
    again = false
    // Declared first so the `finally` below can compare against it.
    let run: Promise<void> | null = null
    run = (async () => {
      try {
        do {
          again = false
          try {
            const data = await api.get<unknown>(
              `/api/tasks?workspace=${encodeURIComponent(workspace)}`,
            )
            if (inflightWorkspace !== workspace) return
            tasks.value = readableTasks(taskRowsFrom(data))
            loadedWorkspace.value = workspace
          } catch {
            // Keep the last rows: a dropped read must not clear the counts.
          }
        } while (again && inflightWorkspace === workspace)
      } finally {
        if (inflight === run) inflight = null
      }
    })()
    inflight = run
    return run
  }

  /**
   * The task a chat works on. The chat's own `task_delegation` stamp is the
   * durable link; `task.chat_id` covers a chat whose stamp the client has not
   * received yet. Only tasks in the loaded workspace resolve.
   */
  function taskForChat(chat: Pick<ChatInfo, 'chat_id' | 'helper'> | null | undefined): Task | null {
    if (!chat) return null
    const helper = chat.helper
    if (helper?.kind === 'task_delegation') {
      const byId = tasks.value.find(t => t.id === helper.task_id)
      if (byId) return byId
    }
    return tasks.value.find(t => t.chat_id === chat.chat_id) ?? null
  }

  /** The agent finished and the result waits for the user's Approve. */
  function isAwaitingReview(task: Task): boolean {
    return task.status === 'in_review' && task.attempt_state === 'ready_for_review' && !!task.live_attempt_id
  }

  /**
   * The agent stopped and asked for the user — it reported needs input or
   * blocked, or ended without a report. The reply goes in the chat.
   */
  function isWaitingOnUser(task: Task): boolean {
    return task.status !== 'done' && task.attempt_state === 'needs_you' && !!task.live_attempt_id
  }

  /** Tasks the agent reported done that wait for review, in `workspace`. */
  function inReviewCount(workspace: string): number {
    if (workspace !== loadedWorkspace.value) return 0
    return tasks.value.filter(isAwaitingReview).length
  }

  function chatTaskWaitingOnUser(chat: Pick<ChatInfo, 'chat_id' | 'helper'>): boolean {
    const task = taskForChat(chat)
    return !!task && isWaitingOnUser(task)
  }

  return {
    tasks,
    loadedWorkspace,
    reload,
    taskForChat,
    isAwaitingReview,
    isWaitingOnUser,
    inReviewCount,
    chatTaskWaitingOnUser,
  }
})
