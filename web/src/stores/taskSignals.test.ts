// @vitest-environment jsdom

/**
 * The app-wide read of delegated tasks: which chat a task runs in, which tasks
 * wait for review or for the user, and a reload that neither blanks the signals
 * on a failed read nor loses a change that lands while a read is in flight.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
import { api } from '../lib/api'
import { useTaskSignalsStore } from './taskSignals'
import type { ChatInfo, Task } from '../lib/types'

const get = vi.mocked(api.get)

const ATTEMPT_ID = 'a'.repeat(32)

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'in_progress',
    project_id: '',
    due: '',
    assignee: 'agent',
    chat_id: '',
    attempt_id: '',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: 'r'.repeat(64),
    relative_path: 'Tasks/ship.md',
    attempt_state: '',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    changed_since_delegated: false,
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

function delegated(overrides: Partial<Task> = {}): Task {
  return task({
    chat_id: 'chat-1',
    attempt_id: ATTEMPT_ID,
    live_attempt_id: ATTEMPT_ID,
    attempt_state: 'running',
    ...overrides,
  })
}

function chat(chatId: string, helper?: ChatInfo['helper']): Pick<ChatInfo, 'chat_id' | 'helper'> {
  return { chat_id: chatId, helper }
}

function stamp(taskId: string): ChatInfo['helper'] {
  return { kind: 'task_delegation', task_id: taskId, task_revision: 'r'.repeat(64), attempt_id: ATTEMPT_ID }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (err: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('useTaskSignalsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    get.mockReset()
  })

  it('reads the named workspace and records it as loaded', async () => {
    get.mockResolvedValue({ workspace: 'work', tasks: [delegated()] })
    const store = useTaskSignalsStore()
    await store.reload('work')
    expect(get).toHaveBeenCalledWith('/api/tasks?workspace=work')
    expect(store.loadedWorkspace).toBe('work')
    expect(store.tasks.map(t => t.id)).toEqual(['ship'])
  })

  it('does nothing without a workspace', async () => {
    const store = useTaskSignalsStore()
    await store.reload('')
    expect(get).not.toHaveBeenCalled()
  })

  it('keeps the last rows when a read fails', async () => {
    get.mockResolvedValueOnce({ tasks: [delegated()] })
    const store = useTaskSignalsStore()
    await store.reload('personal')
    get.mockRejectedValueOnce(new Error('offline'))
    await store.reload('personal')
    expect(store.tasks).toHaveLength(1)
  })

  it('reads once more when asked again mid-read, however often it was asked', async () => {
    const first = deferred<unknown>()
    get.mockReturnValueOnce(first.promise as Promise<never>)
    get.mockResolvedValueOnce({ tasks: [delegated({ attempt_state: 'needs_you' })] })
    const store = useTaskSignalsStore()

    const a = store.reload('personal')
    const b = store.reload('personal')
    const c = store.reload('personal')
    first.resolve({ tasks: [delegated()] })
    await Promise.all([a, b, c])

    // The first read may predate the change that prompted the later asks.
    expect(get).toHaveBeenCalledTimes(2)
    expect(store.tasks[0].attempt_state).toBe('needs_you')
  })

  it('drops a read that a workspace switch overtook', async () => {
    const personal = deferred<unknown>()
    get.mockReturnValueOnce(personal.promise as Promise<never>)
    get.mockResolvedValueOnce({ tasks: [delegated({ id: 'work-task' })] })
    const store = useTaskSignalsStore()

    const a = store.reload('personal')
    const b = store.reload('work')
    await b
    personal.resolve({ tasks: [delegated({ id: 'personal-task' })] })
    await a

    expect(store.loadedWorkspace).toBe('work')
    expect(store.tasks.map(t => t.id)).toEqual(['work-task'])
  })

  it('drops another workspace\'s rows as soon as a switch starts', async () => {
    get.mockResolvedValueOnce({ tasks: [delegated({ id: 'personal-task' })] })
    const store = useTaskSignalsStore()
    await store.reload('personal')
    const work = deferred<unknown>()
    get.mockReturnValueOnce(work.promise as Promise<never>)

    const pending = store.reload('work')
    // Slugs collide across workspaces: held rows must not resolve work's chats.
    expect(store.tasks).toEqual([])
    expect(store.loadedWorkspace).toBe('')
    work.resolve({ tasks: [delegated({ id: 'work-task' })] })
    await pending
    expect(store.tasks.map(t => t.id)).toEqual(['work-task'])
  })

  it('drops an older read of the same workspace overtaken by a switch away and back', async () => {
    const stale = deferred<unknown>()
    get.mockReturnValueOnce(stale.promise as Promise<never>)
    get.mockResolvedValueOnce({ tasks: [delegated({ id: 'work-task' })] })
    get.mockResolvedValueOnce({ tasks: [delegated({ id: 'fresh' })] })
    const store = useTaskSignalsStore()

    const a = store.reload('personal')
    await store.reload('work')
    await store.reload('personal')
    stale.resolve({ tasks: [delegated({ id: 'stale' })] })
    await a

    expect(store.tasks.map(t => t.id)).toEqual(['fresh'])
  })

  it('does not report an earlier attempt\'s chat as waiting on the user', async () => {
    const newer = 'b'.repeat(32)
    get.mockResolvedValue({
      tasks: [delegated({ chat_id: 'chat-new', attempt_id: newer, live_attempt_id: newer, attempt_state: 'needs_you' })],
    })
    const store = useTaskSignalsStore()
    await store.reload('personal')

    // The old chat still links back to its task, but the state is the newer attempt's.
    const old = chat('chat-old', stamp('ship'))
    expect(store.taskForChat(old)?.id).toBe('ship')
    expect(store.chatTaskWaitingOnUser(old)).toBe(false)
    expect(store.chatTaskWaitingOnUser(chat('chat-new', { ...stamp('ship')!, attempt_id: newer } as ChatInfo['helper']))).toBe(true)
  })

  it('finds a chat\'s task by its stamp, then by the task\'s chat id', async () => {
    get.mockResolvedValue({
      tasks: [delegated({ id: 'stamped', chat_id: 'other' }), delegated({ id: 'linked', chat_id: 'chat-2' })],
    })
    const store = useTaskSignalsStore()
    await store.reload('personal')

    expect(store.taskForChat(chat('chat-9', stamp('stamped')))?.id).toBe('stamped')
    expect(store.taskForChat(chat('chat-2'))?.id).toBe('linked')
    // A stamp naming a task this workspace does not hold falls back to chat id.
    expect(store.taskForChat(chat('chat-2', stamp('gone')))?.id).toBe('linked')
    expect(store.taskForChat(chat('chat-none'))).toBeNull()
    expect(store.taskForChat(null)).toBeNull()
  })

  it('tells a result waiting for review from one waiting on the user', () => {
    const store = useTaskSignalsStore()
    const review = delegated({ status: 'in_review', attempt_state: 'ready_for_review' })
    const waiting = delegated({ attempt_state: 'needs_you', attempt_outcome: 'blocked' })

    expect(store.isAwaitingReview(review)).toBe(true)
    expect(store.isWaitingOnUser(review)).toBe(false)
    expect(store.isWaitingOnUser(waiting)).toBe(true)
    expect(store.isAwaitingReview(waiting)).toBe(false)
    // Released (approved or detached): no live attempt, nothing to act on.
    expect(store.isAwaitingReview({ ...review, live_attempt_id: '' })).toBe(false)
    expect(store.isWaitingOnUser({ ...waiting, live_attempt_id: '' })).toBe(false)
    // Done is done, whatever the last attempt said.
    expect(store.isWaitingOnUser({ ...waiting, status: 'done' })).toBe(false)
  })

  it('counts review-ready tasks only for the loaded workspace', async () => {
    get.mockResolvedValue({
      tasks: [
        delegated({ id: 'a', status: 'in_review', attempt_state: 'ready_for_review' }),
        delegated({ id: 'b', status: 'in_review', attempt_state: 'ready_for_review' }),
        delegated({ id: 'c', attempt_state: 'needs_you' }),
      ],
    })
    const store = useTaskSignalsStore()
    await store.reload('personal')
    expect(store.inReviewCount('personal')).toBe(2)
    expect(store.inReviewCount('work')).toBe(0)
  })

  it('says whether a chat\'s task stopped for the user', async () => {
    get.mockResolvedValue({
      tasks: [
        delegated({ id: 'waiting', chat_id: 'chat-w', attempt_state: 'needs_you' }),
        delegated({ id: 'running', chat_id: 'chat-r' }),
      ],
    })
    const store = useTaskSignalsStore()
    await store.reload('personal')
    expect(store.chatTaskWaitingOnUser(chat('chat-w'))).toBe(true)
    expect(store.chatTaskWaitingOnUser(chat('chat-r'))).toBe(false)
    expect(store.chatTaskWaitingOnUser(chat('chat-plain'))).toBe(false)
  })
})
