// @vitest-environment jsdom

/**
 * `stores/taskBoard.ts` — the attempt gestures, which have no revision, and the
 * **Send update**, which does.
 *
 * The one thing only the store can decide about a gesture: it acts on an *attempt*,
 * so there is no `expected_revision` to present and nothing to re-plan. What it does
 * owe the caller is the task row it moved, adopted through the same guard as every
 * other write. `attemptAction` used to come back from a `stop` with no `task` at all,
 * and since the pane only reloaded on an error, the card went on reading "Running"
 * over a turn the user had just ended.
 *
 * `sendUpdate` is the other kind of write: it is a *message* into a chat, so it
 * presents the revision the message was composed at — the server rebinds the attempt
 * to it, and the answer's own row carries the flag that rebind cleared, which is
 * what retires the control.
 *
 * The write-safety rules — the revision contract, `drawingWorkspace`, the late
 * answer guards — are exercised where they are decided, in
 * `components/__tests__/TaskBoardView.test.ts`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useTaskBoardStore } from '../taskBoard'
import { readableTasks } from '../../lib/taskBoard'
import type { Task } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'c'.repeat(64)
const ATTEMPT_ID = 'b'.repeat(32)

function running(overrides: Record<string, unknown> = {}): Task {
  return {
    id: 'doing',
    title: 'Wire the store',
    status: 'in_progress',
    assignee: 'agent',
    project_id: '',
    due: '',
    chat_id: 'chat-7',
    attempt_id: ATTEMPT_ID,
    attempt_state: 'running',
    live_attempt_id: ATTEMPT_ID,
    changed_since_delegated: false,
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: REVISION,
    relative_path: 'Tasks/doing.md',
    ...overrides,
  } as Task
}

/** The one readable row, which is what the assertions are about. */
function row(board: ReturnType<typeof useTaskBoardStore>): Task {
  const [found] = readableTasks(board.rows)
  if (!found) throw new Error('the board drew no readable row')
  return found
}

describe('taskBoard attempt gestures', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    apiGet.mockResolvedValue({ workspace: 'personal', tasks: [running()] })
  })

  it('adopts the row a stop reply carries, so the card leaves Running', async () => {
    const board = useTaskBoardStore()
    await board.reload('personal')
    expect(row(board).attempt_state).toBe('running')

    apiPost.mockResolvedValue({
      workspace: 'personal',
      attempt: { attempt_id: ATTEMPT_ID, state: 'stopped', live: false, chat_id: 'chat-7' },
      chat_id: 'chat-7',
      // The reply the service writes for a stop: the attempt *and* the task.
      task: { ...running({ attempt_state: 'stopped', live_attempt_id: '', revision: NEXT_REVISION }), body: '' },
    })

    const attempt = await board.attemptAction('personal', 'doing', ATTEMPT_ID, 'stop')

    expect(apiPost).toHaveBeenCalledWith(
      `/api/tasks/doing/attempt/${ATTEMPT_ID}/stop`,
      { workspace: 'personal' },
    )
    expect(attempt?.state).toBe('stopped')
    // The row is refreshed from the answer, with no second read: a live attempt
    // that is gone is what tells the card Stop and Detach are no longer available.
    expect(row(board).attempt_state).toBe('stopped')
    expect(row(board).live_attempt_id).toBe('')
    expect(row(board).revision).toBe(NEXT_REVISION)
    expect(board.error).toBe('')
  })

  it('leaves the row alone when the gesture fails, and says why', async () => {
    const board = useTaskBoardStore()
    await board.reload('personal')
    apiPost.mockRejectedValue(Object.assign(new Error('HTTP 400'), {
      payload: {
        error: {
          code: 'invalid_action',
          message: "attempt is 'ready_for_review'; only an attempt that did not finish can be resumed.",
          retryable: false,
        },
      },
    }))

    const attempt = await board.attemptAction('personal', 'doing', ATTEMPT_ID, 'resume')

    expect(attempt).toBeNull()
    expect(board.error).toContain('only an attempt that did not finish')
    expect(row(board).attempt_state).toBe('running')
  })

  it('sends an update at the revision the preview was read at, and adopts the rebind',
    async () => {
      const board = useTaskBoardStore()
      const edited = running({ changed_since_delegated: true, revision: NEXT_REVISION })
      await board.reload('personal')
      board.rows.splice(0, board.rows.length, edited)
      apiPost.mockResolvedValue({
        workspace: 'personal',
        updated: true,
        queued: false,
        chat_id: 'chat-7',
        attempt: { attempt_id: ATTEMPT_ID, state: 'running', live: true, chat_id: 'chat-7' },
        // The rebind is the point: the answer's own row carries the flag cleared, so
        // the pane's control retires without a second read.
        task: { ...edited, changed_since_delegated: false, revision: REVISION },
      })

      const attempt = await board.sendUpdate(
        'personal', 'doing', ATTEMPT_ID, NEXT_REVISION, 'The task now reads differently.',
      )

      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/doing/attempt/${ATTEMPT_ID}/update`,
        {
          workspace: 'personal',
          expected_revision: NEXT_REVISION,
          message: 'The task now reads differently.',
        },
      )
      expect(attempt?.attempt_id).toBe(ATTEMPT_ID)
      expect(row(board).changed_since_delegated).toBe(false)
      expect(board.error).toBe('')
    })

  it('refuses a send it cannot make, rather than reporting a delivery', async () => {
    const board = useTaskBoardStore()
    await board.reload('personal')
    apiPost.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: {
        error: {
          code: 'task_update_busy',
          message: 'the delegated chat is finishing a turn and cannot take the update yet',
          retryable: true,
        },
      },
    }))

    const attempt = await board.sendUpdate('personal', 'doing', ATTEMPT_ID, REVISION, 'Later.')

    expect(attempt).toBeNull()
    expect(board.error).toContain('cannot take the update yet')
    // The row is untouched, so the flag the control reads still stands and the user
    // is offered the update again.
    expect(row(board).changed_since_delegated).toBe(false)
  })

  it('sends nothing without an attempt, a revision or a message', async () => {
    const board = useTaskBoardStore()
    await board.reload('personal')

    expect(await board.sendUpdate('personal', 'doing', '', REVISION, 'x')).toBeNull()
    expect(await board.sendUpdate('personal', 'doing', ATTEMPT_ID, '', 'x')).toBeNull()
    expect(await board.sendUpdate('personal', 'doing', ATTEMPT_ID, REVISION, '   ')).toBeNull()
    expect(await board.sendUpdate('', 'doing', ATTEMPT_ID, REVISION, 'x')).toBeNull()
    expect(apiPost).not.toHaveBeenCalled()
  })
})