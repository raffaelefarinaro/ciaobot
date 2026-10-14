import { beforeEach, describe, expect, it, vi } from 'vitest'
import { approveTask, markTaskDone, taskClosureErrorMessage } from './taskClosure'
import type { Task } from './types'

const apiPost = vi.hoisted(() => vi.fn())
vi.mock('./api', () => ({ api: { post: apiPost } }))

const REVISION = 'a'.repeat(64)
const FRESH = 'b'.repeat(64)

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'in_progress',
    project_id: '',
    due: '',
    assignee: 'agent',
    chat_id: 'chat-1',
    attempt_id: 'att-1',
    linked_chats: [],
    created_at: '',
    updated_at: '',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    attempt_state: 'running',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: 'att-1',
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

describe('taskClosure', () => {
  beforeEach(() => {
    apiPost.mockReset()
    apiPost.mockResolvedValue({})
  })

  it('approveTask completes at the revision read', async () => {
    await approveTask(task(), 'personal', 'Shipped')
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
      resolution: 'Shipped',
    })
  })

  it('markTaskDone detaches the live attempt, then completes at the revision the detach returned', async () => {
    apiPost.mockResolvedValueOnce({ task: { revision: FRESH } })
    await markTaskDone(task(), 'personal', 'Done by hand')
    expect(apiPost).toHaveBeenNthCalledWith(1, '/api/tasks/ship/attempt/att-1/detach', { workspace: 'personal' })
    expect(apiPost).toHaveBeenNthCalledWith(2, '/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: FRESH,
      resolution: 'Done by hand',
    })
  })

  it('markTaskDone skips the detach when no attempt holds the task', async () => {
    await markTaskDone(task({ attempt_id: '', live_attempt_id: '' }), 'personal', '')
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
      resolution: '',
    })
  })

  it('taskClosureErrorMessage words a 409 per kind and falls back otherwise', () => {
    expect(taskClosureErrorMessage({ status: 409 }, 'approve')).toBe(
      'This task changed since it was read. Check it and approve again.',
    )
    expect(taskClosureErrorMessage({ status: 409 }, 'mark-done')).toBe(
      'This task changed since it was read. Check it and try again.',
    )
    expect(taskClosureErrorMessage(null, 'approve')).toBe('Could not approve the task')
    expect(taskClosureErrorMessage(null, 'mark-done')).toBe('Could not mark the task done')
  })
})
