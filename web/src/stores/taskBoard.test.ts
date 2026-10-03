// @vitest-environment jsdom

/**
 * The task board's write paths, across a workspace switch.
 *
 * The one guarantee under test is that a write's answer is only ever adopted onto
 * the board it was made for. A status move or a Save goes out, the user presses a
 * digit and `1`–`9` switch workspace, and the PATCH lands afterwards: the answer
 * is a record of the workspace just left, so adopting it would draw another
 * workspace's task id and revision under the new name — and the next write from
 * that card would present those ids to a vault that has never heard of them.
 * The same hold applies to a create's description and to a delete's row filter.
 *
 * The switch is the only thing under test; the reads and the refusal paths are
 * the store's existing contract and `TaskBoardView.test.ts` covers them mounted.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
import { api } from '../lib/api'
import { readableTasks } from '../lib/taskBoard'
import { useTaskBoardStore } from './taskBoard'
import type { Task, TaskDetail } from '../lib/types'

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)
const patch = vi.mocked(api.patch)
const del = vi.mocked(api.del)

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'b'.repeat(64)

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'backlog',
    project_id: '',
    due: '',
    assignee: 'user',
    review_state: 'none',
    chat_id: '',
    attempt_id: '',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    ...overrides,
  }
}

/** A write's answer: the record as stored, description included. */
function stored(overrides: Partial<TaskDetail> = {}): { task: TaskDetail } {
  return { task: { ...task(), revision: NEXT_REVISION, body: '', ...overrides } }
}

/** A promise the test resolves by hand, so a write can be held in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((settle) => { resolve = settle })
  return { promise, resolve }
}

/** Load `personal` with one row, then move the board to `work`, which is empty. */
async function switchAway(): Promise<void> {
  const store = useTaskBoardStore()
  get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
  await store.reload('personal')
  expect(store.loadedWorkspace).toBe('personal')
  get.mockResolvedValue({ workspace: 'work', tasks: [] })
  await store.reload('work')
  expect(store.loadedWorkspace).toBe('work')
  expect(store.rows).toHaveLength(0)
}

describe('taskBoard store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('adopts a write answer while the board still draws the workspace it was made for', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
    await store.reload('personal')
    patch.mockResolvedValue(stored({ status: 'in_progress' }))

    const saved = await store.update('personal', 'ship', REVISION, { status: 'in_progress' })

    expect(saved?.status).toBe('in_progress')
    expect(readableTasks(store.rows)[0]!.status).toBe('in_progress')
    expect(readableTasks(store.rows)[0]!.revision).toBe(NEXT_REVISION)
    expect(store.error).toBe('')
  })

  it('drops a PATCH answer that arrives after the workspace has changed', async () => {
    const store = useTaskBoardStore()
    await switchAway()
    const inFlight = deferred<{ task: TaskDetail }>()
    patch.mockReturnValue(inFlight.promise)

    const pending = store.update('personal', 'ship', REVISION, { status: 'in_progress' })
    inFlight.resolve(stored({ status: 'in_progress' }))

    // Nothing failed, so nothing is said to have failed; the write did land on
    // disk, and the answer is simply not this board's to draw.
    expect(await pending).toBeNull()
    expect(store.rows).toHaveLength(0)
    expect(store.described).toBeNull()
    expect(store.error).toBe('')
    expect(store.saving).toBe(false)
  })

  it('drops a POST answer that arrives after the workspace has changed', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [] })
    await store.reload('personal')
    const inFlight = deferred<{ task: TaskDetail }>()
    post.mockReturnValue(inFlight.promise)

    const pending = store.create('personal', { title: 'Filed while away' })
    get.mockResolvedValue({ workspace: 'work', tasks: [] })
    await store.reload('work')
    inFlight.resolve(stored({ id: 'filed', title: 'Filed while away' }))

    // A create has no row to replace, so `adopt` would push one: a task the new
    // workspace never listed, plus a description slot filled from it.
    expect(await pending).toBeNull()
    expect(store.rows).toHaveLength(0)
    expect(store.described).toBeNull()
    expect(store.error).toBe('')
  })

  it('drops a completion answer that arrives after the workspace has changed', async () => {
    const store = useTaskBoardStore()
    await switchAway()
    const inFlight = deferred<{ task: TaskDetail }>()
    post.mockReturnValue(inFlight.promise)

    const pending = store.complete('personal', 'ship', REVISION)
    inFlight.resolve(stored({ status: 'done' }))

    expect(await pending).toBeNull()
    expect(store.rows).toHaveLength(0)
    expect(store.described).toBeNull()
  })

  it('does not let a late delete drop a real row on the new board', async () => {
    const store = useTaskBoardStore()
    await switchAway()
    // Slugs collide across workspaces, so the new board can hold the same id.
    get.mockResolvedValue({
      workspace: 'work',
      tasks: [task({ id: 'ship', title: "Work's own ship" })],
    })
    await store.reload('work')
    const inFlight = deferred<{ workspace: string; deleted: string }>()
    del.mockReturnValue(inFlight.promise)

    const pending = store.remove('personal', 'ship', REVISION)
    inFlight.resolve({ workspace: 'personal', deleted: 'ship' })

    expect(await pending).toBe(false)
    // `TaskRow` is the readable task or the "not a task file" row, so the board is
    // read the way the pane reads it.
    expect(readableTasks(store.rows).map((row) => row.id)).toEqual(['ship'])
    expect(readableTasks(store.rows)[0]!.title).toBe("Work's own ship")
    expect(store.error).toBe('')
  })
})