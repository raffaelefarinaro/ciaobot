import { api } from './api'
import { taskApiErrorMessage } from './taskBoard'
import type { Task } from './types'

export type TaskClosureKind = 'approve' | 'mark-done'

/** Approve a result waiting on review: complete at the revision read. The server archives the chat. */
export async function approveTask(task: Task, workspace: string, resolution: string): Promise<void> {
  await api.post(`/api/tasks/${encodeURIComponent(task.id)}/complete`, {
    workspace,
    expected_revision: task.revision,
    resolution,
  })
}

/** Override: detach the live attempt (if any), then complete at the revision the detach returned. */
export async function markTaskDone(task: Task, workspace: string, resolution: string): Promise<void> {
  let revision = task.revision
  const attemptId = task.live_attempt_id || task.attempt_id
  if (attemptId) {
    const freed = await api.post<{ task?: { revision?: string } }>(
      `/api/tasks/${encodeURIComponent(task.id)}/attempt/${encodeURIComponent(attemptId)}/detach`,
      { workspace },
    )
    revision = freed?.task?.revision || revision
  }
  await api.post(`/api/tasks/${encodeURIComponent(task.id)}/complete`, {
    workspace,
    expected_revision: revision,
    resolution,
  })
}

export function taskClosureErrorMessage(e: unknown, kind: TaskClosureKind): string {
  if ((e as { status?: number } | null)?.status === 409) {
    return kind === 'approve'
      ? 'This task changed since it was read. Check it and approve again.'
      : 'This task changed since it was read. Check it and try again.'
  }
  return taskApiErrorMessage(e, kind === 'approve' ? 'Could not approve the task' : 'Could not mark the task done')
}
