// @vitest-environment jsdom

/**
 * The peek overlay's task read. A peek over a chat must show the task of the
 * workspace on screen, drop any answer that a newer open or a close has
 * overtaken, and survive a live re-read without blanking what it shows.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('./projects', async () => {
  const { ref: makeRef } = await import('vue')
  const activeWorkspace = makeRef('personal')
  return {
    useProjectStore: () => ({
      get activeWorkspace() {
        return activeWorkspace.value
      },
    }),
    __activeWorkspace: activeWorkspace,
  }
})

import { api } from '../lib/api'
import * as projects from './projects'
import { useItemPeekStore } from './itemPeek'
import { useTaskSignalsStore } from './taskSignals'
import type { Task } from '../lib/types'

const get = vi.mocked(api.get)
const activeWorkspace = (projects as unknown as { __activeWorkspace: ReturnType<typeof ref<string>> }).__activeWorkspace

const REVISION = 'a'.repeat(64)

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
    linked_chats: [],
    created_at: '',
    updated_at: '',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    attempt_state: '',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

function answer(overrides: Partial<Task> = {}): { task: Record<string, unknown> } {
  return {
    task: {
      ...task(overrides),
      body: '',
      resolution: '',
      completions: [],
      delegation_log: '',
      updates: [],
    },
  }
}

/** A promise the test resolves by hand, so a read can be held in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (e: unknown) => void } {
  let resolve!: (value: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function httpError(status: number, payload?: unknown): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status, payload })
}

beforeEach(() => {
  setActivePinia(createPinia())
  get.mockReset()
  activeWorkspace.value = 'personal'
})

describe('itemPeek store', () => {
  it('reads the task in the active workspace', async () => {
    get.mockResolvedValueOnce(answer({ title: 'Ship the board' }))
    const peek = useItemPeekStore()
    peek.openTask('ship')
    expect(peek.isOpen).toBe(true)
    expect(peek.loading).toBe(true)
    await vi.waitFor(() => expect(peek.task?.title).toBe('Ship the board'))
    expect(get).toHaveBeenCalledWith('/api/tasks/ship?workspace=personal')
    expect(peek.loading).toBe(false)
    expect(peek.notFound).toBe(false)
    expect(peek.error).toBe('')
  })

  it('a 404 is not-found, not an error', async () => {
    get.mockRejectedValueOnce(httpError(404))
    const peek = useItemPeekStore()
    peek.openTask('gone')
    await vi.waitFor(() => expect(peek.notFound).toBe(true))
    expect(peek.error).toBe('')
    expect(peek.task).toBeNull()
  })

  it('another failure keeps the server\'s sentence', async () => {
    get.mockRejectedValueOnce(httpError(500, { error: { code: 'locked', message: 'The task is being edited' } }))
    const peek = useItemPeekStore()
    peek.openTask('ship')
    await vi.waitFor(() => expect(peek.error).toBe('The task is being edited'))
    expect(peek.notFound).toBe(false)
  })

  it('a late answer for an earlier task is dropped', async () => {
    const first = deferred<unknown>()
    const second = deferred<unknown>()
    get.mockReturnValueOnce(first.promise as Promise<never>).mockReturnValueOnce(second.promise as Promise<never>)
    const peek = useItemPeekStore()
    peek.openTask('first')
    peek.openTask('second')
    second.resolve(answer({ id: 'second', title: 'Second' }))
    await vi.waitFor(() => expect(peek.task?.id).toBe('second'))
    first.resolve(answer({ id: 'first', title: 'First' }))
    await Promise.resolve()
    await Promise.resolve()
    expect(peek.task?.id).toBe('second')
  })

  it('close drops an answer still in flight', async () => {
    const pending = deferred<unknown>()
    get.mockReturnValueOnce(pending.promise as Promise<never>)
    const peek = useItemPeekStore()
    peek.openTask('ship')
    peek.close()
    expect(peek.isOpen).toBe(false)
    pending.resolve(answer())
    await Promise.resolve()
    await Promise.resolve()
    expect(peek.task).toBeNull()
    expect(peek.isOpen).toBe(false)
  })

  it('a workspace switch closes the peek', async () => {
    get.mockResolvedValueOnce(answer())
    const peek = useItemPeekStore()
    peek.openTask('ship')
    await vi.waitFor(() => expect(peek.task).not.toBeNull())
    activeWorkspace.value = 'work'
    await nextTick()
    expect(peek.isOpen).toBe(false)
    expect(peek.task).toBeNull()
  })

  it('a tasks_changed re-read refreshes the open task without blanking it', async () => {
    get.mockResolvedValueOnce(answer({ title: 'Before' }))
    const peek = useItemPeekStore()
    const signals = useTaskSignalsStore()
    peek.openTask('ship')
    await vi.waitFor(() => expect(peek.task?.title).toBe('Before'))

    const reread = deferred<unknown>()
    get.mockReturnValueOnce(reread.promise as Promise<never>)
    signals.tasks = [task({ title: 'After' })]
    await nextTick()
    expect(get).toHaveBeenCalledTimes(2)
    expect(peek.task?.title).toBe('Before')
    expect(peek.loading).toBe(false)

    reread.resolve(answer({ title: 'After' }))
    await vi.waitFor(() => expect(peek.task?.title).toBe('After'))
  })
})
