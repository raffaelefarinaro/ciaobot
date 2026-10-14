import { ref } from 'vue'
import type { Task } from './types'

/** approved: the server archives the chat. marked-done / keep: the caller archives. */
export type ArchiveTaskChoice = 'approved' | 'marked-done' | 'keep' | 'cancel'

export interface ArchiveTaskRequest {
  task: Task
  workspace: string
  awaitingReview: boolean
  resolve: (choice: ArchiveTaskChoice) => void
}

export const pendingArchiveTask = ref<ArchiveTaskRequest | null>(null)

/**
 * Ask what to do with the open task a chat holds before archiving it. Same
 * shape as `askConfirm`: rendered once by `ArchiveTaskDialog`, awaited by callers.
 */
export function askArchiveTask(
  task: Task,
  workspace: string,
  awaitingReview: boolean,
): Promise<ArchiveTaskChoice> {
  // A second request would orphan the first one's promise; settle it as cancelled.
  pendingArchiveTask.value?.resolve('cancel')
  return new Promise<ArchiveTaskChoice>(resolve => {
    let settled = false
    pendingArchiveTask.value = {
      task,
      workspace,
      awaitingReview,
      resolve: (choice: ArchiveTaskChoice) => {
        if (settled) return
        settled = true
        pendingArchiveTask.value = null
        resolve(choice)
      },
    }
  })
}
