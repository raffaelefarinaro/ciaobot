// @vitest-environment jsdom
//
// The "After this update" half of the housekeeping store: the second cheap
// request, the per-state transitions, and what each of them is allowed to do to
// the list.
//
// The claims these pin are the ones a card silently breaks:
//
//  - a refusal never clears a card. A start that is declined, a dismiss that
//    409s and a reopen of something that is not hidden all leave the row exactly
//    where it was, with the reason attached. A cleared card reads as "handled",
//    and none of those three are.
//  - a start that failed *after* the chat exists keeps the chat id. The server
//    goes out of its way to send it back because the next start has to retry
//    into that same chat; dropping it would strand a chat the engine already
//    made and lose the task's only attempt.
//  - a switch of workspace drops the previous workspace's rows rather than
//    showing them under the new one.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api'
import { useHousekeepingStore } from './housekeeping'
import { useProjectStore } from './projects'
import type { UpdateTaskRow, UpdateTasksResponse } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}))

/** A refused call shaped the way `lib/api.ts` throws one. */
function refused(status: number, body: Record<string, unknown>): Error {
  return Object.assign(new Error(String(body.error ?? 'refused')), {
    name: 'ApiError',
    status,
    payload: body,
  })
}

function task(overrides: Partial<UpdateTaskRow> = {}): UpdateTaskRow {
  return {
    id: 'review-legacy-rows',
    revision: 1,
    scope: 'workspace',
    title: 'Review the rows your old notes left behind',
    why: 'Rows retired into a folder no surface reads any more.',
    since_version: '2.1.0',
    status: 'offered',
    applicability: 'applicable',
    applicability_checked_at: '2026-09-01T10:00:00+00:00',
    offered: true,
    suppressed: false,
    chat_id: '',
    prompt_digest: '',
    attempted_fingerprint: '',
    updated_at: '',
    ...overrides,
  }
}

function listing(rows: UpdateTaskRow[]): UpdateTasksResponse {
  return { tasks: rows }
}

beforeEach(() => {
  setActivePinia(createPinia())
  useProjectStore().activeWorkspace = 'personal'
  vi.mocked(api.get).mockReset()
  vi.mocked(api.post).mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the second request', () => {
  it('asks per workspace, on its own path, with the workspace named', async () => {
    // Applicability and state are per workspace and the route refuses a
    // nameless question, so this cannot ride along on /api/housekeeping: that
    // request has no workspace to put in the query.
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    const store = useHousekeepingStore()
    await store.refreshUpdateTasks('personal')

    expect(api.get).toHaveBeenCalledWith('/api/update-tasks?workspace=personal')
    expect(store.updateTasks.map((row) => row.id)).toEqual(['review-legacy-rows'])
  })

  it('does not ask at all without a workspace', async () => {
    // A nameless question would be answered with a 400, and rendering that
    // refusal as "no tasks" would be a claim nobody made about anybody.
    useProjectStore().activeWorkspace = ''
    const store = useHousekeepingStore()
    await store.refreshUpdateTasks()

    expect(api.get).not.toHaveBeenCalled()
    expect(store.updateTasks).toEqual([])
  })

  it('reads coverage_gap when nothing is offered, and clears it when it is', async () => {
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValueOnce({
      tasks: [task({ offered: false, applicability: 'not_applicable' })],
      coverage_gap: { reason: 'nothing_offered', detail: 'no eligible task applies right now' },
    } as never)
    await store.refreshUpdateTasks('personal')
    expect(store.updateTasksCoverageGap?.reason).toBe('nothing_offered')

    vi.mocked(api.get).mockResolvedValueOnce(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    expect(store.updateTasksCoverageGap).toBeNull()
  })

  it('keeps the previous rows and stays silent when a refresh fails', async () => {
    // Same best-effort contract as the strip above it: a background poll on the
    // home screen does not raise a red line about something the operator cannot
    // act on from this page. The rows stay, because a failed refresh is not a
    // cleared list — clearing would claim this workspace has no tasks when the
    // truth is that nobody could ask.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValueOnce(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')

    vi.mocked(api.get).mockRejectedValueOnce(new Error('offline'))
    await store.refreshUpdateTasks('personal')

    expect(store.updateTasks).toHaveLength(1)
  })

  it("drops another workspace's rows when the workspace changes", async () => {
    // Showing the previous workspace's answers under the new one is worse than a
    // briefly empty list: the card would be right about the wrong workspace.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValueOnce(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    expect(store.updateTasks).toHaveLength(1)

    vi.mocked(api.get).mockResolvedValueOnce(listing([]) as never)
    await store.refreshUpdateTasks('work')
    expect(store.updateTasks).toEqual([])
  })

  it('discards an answer that lost a workspace race', async () => {
    // The store answers whichever request it started with. Two overlapping
    // refreshes across a switch must not leave the older one on screen.
    const store = useHousekeepingStore()
    // Typed rather than inferred: the assignment happens inside a callback, so
    // control-flow analysis would otherwise narrow the `let` to `never` and the
    // call below would not type-check.
    const pending: { release: (() => void) | null } = { release: null }
    vi.mocked(api.get).mockImplementationOnce(() => new Promise((resolve) => {
      pending.release = () => resolve(listing([task()]) as never)
    }))
    const slow = store.refreshUpdateTasks('personal')

    vi.mocked(api.get).mockResolvedValueOnce(listing([task({ id: 'other' })]) as never)
    await store.refreshUpdateTasks('work')
    expect(store.updateTasks.map((row) => row.id)).toEqual(['other'])

    pending.release?.()
    await slow
    expect(store.updateTasks.map((row) => row.id)).toEqual(['other'])
  })
})

describe('starting a task', () => {
  it('posts the start and re-renders from the rows the reply carried', async () => {
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')

    vi.mocked(api.post).mockResolvedValue({
      ok: true,
      task_id: 'review-legacy-rows',
      chat_id: 'chat-9',
      resumed: false,
      tasks: [task({ status: 'in_progress', chat_id: 'chat-9' })],
    } as never)

    const outcome = await store.startUpdateTask(task())
    expect(outcome).toMatchObject({ ok: true, chatId: 'chat-9', resumed: false })
    expect(api.post).toHaveBeenCalledWith(
      '/api/update-tasks/review-legacy-rows/start?workspace=personal',
      undefined,
    )
    expect(store.updateTasks[0].status).toBe('in_progress')
  })

  it('reports resumed=true when a second press created nothing', async () => {
    // The button is one button on purpose: the server is idempotent per
    // (task, revision), so a double press, a second tab and a retry after a
    // dropped response all land in the same chat with the prompt sent once.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockResolvedValue({
      ok: true, task_id: 'review-legacy-rows', chat_id: 'chat-9', resumed: true,
    } as never)

    const outcome = await store.startUpdateTask(task())
    expect(outcome).toMatchObject({ ok: true, chatId: 'chat-9', resumed: true })
  })

  it('keeps a 500 with a chat_id, because the retry must reuse that chat', async () => {
    // The chat exists and the turn never started. This is the one failure where
    // throwing the id away would strand a chat the engine already made *and*
    // leave the next start to mint a second one.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockRejectedValueOnce(
      refused(500, { error: 'the turn could not be dispatched', chat_id: 'chat-9' }),
    )

    const outcome = await store.startUpdateTask(task())
    expect(outcome.ok).toBe(false)
    expect(outcome.chatId).toBe('chat-9')
    expect(outcome.error).toContain('reuses that same chat')
    expect(store.updateTasks).toHaveLength(1)
  })

  it('surfaces a refusal in plain language and keeps the card', async () => {
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockRejectedValueOnce(
      refused(409, {
        error: "update task 'review-legacy-rows'@1 is already completed at this revision",
      }),
    )

    const outcome = await store.startUpdateTask(task())
    expect(outcome.ok).toBe(false)
    expect(store.taskErrors['review-legacy-rows@1']).toContain('already marked done')
    expect(store.updateTasks).toHaveLength(1)
  })

  it('tells a newer-engine refusal to update rather than to retry', async () => {
    // A task that arrived in a later version is not a failure to retry; sending
    // somebody to "try again" for it would be the wrong next step.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockRejectedValueOnce(
      refused(409, {
        error: "update task 'review-legacy-rows'@1 needs engine 2.2.0 or newer; this install is 2.1.0",
      }),
    )

    const outcome = await store.startUpdateTask(task())
    expect(outcome.error).toContain('Update the engine')
  })

  it('rejects a success that opened no chat rather than clearing the card', async () => {
    // ok with no chat_id is not a launch. Clearing the card on it would tell
    // the operator the work is under way when nothing was opened.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockResolvedValue({ ok: true, task_id: 'review-legacy-rows' } as never)

    const outcome = await store.startUpdateTask(task())
    expect(outcome.ok).toBe(false)
    expect(store.taskErrors['review-legacy-rows@1']).toContain('did not open a chat')
    expect(store.updateTasks).toHaveLength(1)
  })

  it('re-lists rather than showing an empty group when the reply carried no rows', async () => {
    // `tasks` absent is not `tasks: []`. The route leaves the key off when its
    // decision landed but the follow-up listing could not be computed, and
    // writing [] would tell the card this workspace has no tasks.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockResolvedValue({ ok: true, task_id: 'x', chat_id: 'chat-9' } as never)

    await store.startUpdateTask(task())
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('marks the row pending for the duration and clears it afterwards', async () => {
    // A card with a dead button and no feedback is the other half of "silently
    // cleared": the operator cannot tell a press from a no-op.
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')

    let seen: boolean | undefined
    vi.mocked(api.post).mockImplementation(() => new Promise((resolve) => {
      seen = store.pendingUpdateTaskIds.has('review-legacy-rows@1')
      resolve({ ok: true, task_id: 'x', chat_id: 'chat-9' } as never)
    }))

    await store.startUpdateTask(task())
    expect(seen).toBe(true)
    expect(store.pendingUpdateTaskIds.has('review-legacy-rows@1')).toBe(false)
  })

  it('refuses to act without a workspace and says why', async () => {
    const store = useHousekeepingStore()
    const outcome = await store.startUpdateTask(task())
    expect(api.post).not.toHaveBeenCalled()
    expect(outcome.error).toContain('No workspace is selected')
  })
})

describe('dismissing a task', () => {
  it('records the decision and lets the server re-offer nothing for it', async () => {
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task()]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockResolvedValue({
      ok: true, task_id: 'review-legacy-rows', tasks: [task({ status: 'dismissed', suppressed: true })],
    } as never)

    const outcome = await store.dismissUpdateTask(task())
    expect(outcome.ok).toBe(true)
    expect(api.post).toHaveBeenCalledWith(
      '/api/update-tasks/review-legacy-rows/dismiss?workspace=personal',
      { reason: '' },
    )
    // Suppressed, not completed: the row is hidden in this scope and the record
    // is still there to reopen. A card must not read "done" off this.
    expect(store.updateTasks[0].status).toBe('dismissed')
    expect(store.updateTasks[0].suppressed).toBe(true)
  })

  it('keeps the card and the chat when the dismissal is refused', async () => {
    // A record that already says completed is refused, because overwriting a
    // verdict would put a later start back in reach of finished work.
    const store = useHousekeepingStore()
    const running = task({ status: 'in_progress', chat_id: 'chat-9' })
    vi.mocked(api.get).mockResolvedValue(listing([running]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockRejectedValueOnce(
      refused(409, { error: 'update task review-legacy-rows@1 is already completed at this revision' }),
    )

    const outcome = await store.dismissUpdateTask(running)
    expect(outcome.ok).toBe(false)
    expect(store.updateTasks[0].status).toBe('in_progress')
    expect(store.updateTasks[0].chat_id).toBe('chat-9')
  })
})

describe('reopening a task', () => {
  it('re-offers it and reports the lifecycle it is actually in', async () => {
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task({ status: 'dismissed', suppressed: true })]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockResolvedValue({
      ok: true, task_id: 'review-legacy-rows', tasks: [task({ status: 'offered' })],
    } as never)

    const outcome = await store.reopenUpdateTask(task({ status: 'dismissed' }))
    expect(outcome.ok).toBe(true)
    expect(api.post).toHaveBeenCalledWith(
      '/api/update-tasks/review-legacy-rows/reopen?workspace=personal',
      undefined,
    )
    expect(store.updateTasks[0].status).toBe('offered')
  })

  it('keeps the row when the reopen is refused', async () => {
    const store = useHousekeepingStore()
    vi.mocked(api.get).mockResolvedValue(listing([task({ status: 'dismissed' })]) as never)
    await store.refreshUpdateTasks('personal')
    vi.mocked(api.post).mockRejectedValueOnce(refused(409, { error: 'nope' }))

    const outcome = await store.reopenUpdateTask(task({ status: 'dismissed' }))
    expect(outcome.ok).toBe(false)
    expect(store.updateTasks).toHaveLength(1)
  })
})

describe('the two clocks', () => {
  it('carries the check time through untouched, beside the record time', async () => {
    // `Checked` is when a detector last produced this row's answer; `Decided` is
    // when the record was written. A history that merged them would claim a
    // re-check that never happened, or hide a decision that did.
    const store = useHousekeepingStore()
    const row = task({
      status: 'dismissed',
      applicability: 'not_applicable',
      applicability_checked_at: '2026-09-01T10:00:00+00:00',
      updated_at: '2026-09-05T18:30:00+00:00',
    })
    vi.mocked(api.get).mockResolvedValue(listing([row]) as never)
    await store.refreshUpdateTasks('personal')

    expect(store.updateTasks[0].applicability_checked_at).toBe('2026-09-01T10:00:00+00:00')
    expect(store.updateTasks[0].updated_at).toBe('2026-09-05T18:30:00+00:00')
  })
})
