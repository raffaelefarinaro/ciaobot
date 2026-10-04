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
import type { Task, TaskAttempt, TaskDetail } from '../lib/types'

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)
const patch = vi.mocked(api.patch)
const del = vi.mocked(api.del)

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'b'.repeat(64)
const ATTEMPT_ID = 'a'.repeat(32)

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
    attempt_state: '',
    live_attempt_id: '',
    changed_since_delegated: false,
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

  // ── Delegation (#1033) ───────────────────────────────────────────────────
  //
  // The same guard, on the two writes that cross a workspace switch most easily:
  // a delegation names this workspace's task and this workspace's project, and a
  // detach *removes* a row from the board. A late answer for either would draw
  // another workspace's attempt on the board now on screen, or filter a real row of
  // the new board by an id that collides across workspaces.

  /** One attempt, as `POST /delegate` answers with it. */
  function attempt(overrides: Partial<TaskAttempt> = {}): TaskAttempt {
    return {
      attempt_id: ATTEMPT_ID,
      task_id: 'ship',
      task_revision: REVISION,
      chat_id: 'chat-9',
      state: 'running',
      created_at: '2026-03-02T09:00:00+00:00',
      updated_at: '2026-03-02T09:00:00+00:00',
      ended_at: '',
      detail: '',
      released: false,
      live: true,
      ...overrides,
    }
  }

  /** A `POST .../delegate` answer, as the service writes it. */
  function delegated(overrides: Partial<TaskDetail> = {}) {
    return {
      workspace: 'personal',
      created: true,
      attempt: attempt(),
      chat_id: 'chat-9',
      project_id: 'p1',
      project_origin: 'task',
      changed_since_delegated: false,
      task: {
        ...task({
          status: 'in_progress',
          assignee: 'agent',
          chat_id: 'chat-9',
          attempt_id: ATTEMPT_ID,
          attempt_state: 'running',
          live_attempt_id: ATTEMPT_ID,
          revision: NEXT_REVISION,
        }),
        body: '',
        ...overrides,
      },
    }
  }

  it('adopts a delegation at the revision the board holds', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
    await store.reload('personal')
    post.mockResolvedValue(delegated())

    const started = await store.delegate('personal', 'ship', REVISION)

    expect(post).toHaveBeenCalledWith('/api/tasks/ship/delegate', {
      workspace: 'personal',
      expected_revision: REVISION,
    })
    // The badge, the live attempt and the linked chat all arrive with the row: a
    // delegation the board cannot see is a delegation the user cannot stop.
    const row = readableTasks(store.rows)[0]!
    expect(started?.attempt_id).toBe(ATTEMPT_ID)
    expect(row.attempt_state).toBe('running')
    expect(row.live_attempt_id).toBe(ATTEMPT_ID)
    expect(row.chat_id).toBe('chat-9')
    expect(store.error).toBe('')
  })

  it('sends a project override only when one was named', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
    await store.reload('personal')
    post.mockResolvedValue(delegated())

    await store.delegate('personal', 'ship', REVISION, 'p2')

    expect(post).toHaveBeenCalledWith('/api/tasks/ship/delegate', {
      workspace: 'personal',
      expected_revision: REVISION,
      project_id: 'p2',
    })
  })

  it('adopts the attempt a second delegation returned rather than inventing one',
    async () => {
      const store = useTaskBoardStore()
      get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
      await store.reload('personal')
      // `created: false` is the double click: the server returned the attempt that
      // is already running and started nothing.
      post.mockResolvedValue({ ...delegated(), created: false })

      const started = await store.delegate('personal', 'ship', REVISION)

      expect(started?.attempt_id).toBe(ATTEMPT_ID)
      expect(readableTasks(store.rows)[0]!.attempt_state).toBe('running')
      expect(store.error).toBe('')
    })

  it('drops a delegation answer that arrives after the workspace has changed',
    async () => {
      const store = useTaskBoardStore()
      await switchAway()
      const inFlight = deferred<ReturnType<typeof delegated>>()
      post.mockReturnValue(inFlight.promise)

      const pending = store.delegate('personal', 'ship', REVISION)
      inFlight.resolve(delegated())

      // The attempt names this workspace's task, project and chat; adopting it
      // would badge a task the new workspace never listed.
      expect(await pending).toBeNull()
      expect(store.rows).toHaveLength(0)
      expect(store.described).toBeNull()
      expect(store.error).toBe('')
      expect(store.saving).toBe(false)
    })

  it('drops a detach answer that arrives after the workspace has changed', async () => {
    const store = useTaskBoardStore()
    await switchAway()
    const inFlight = deferred<{ attempt: TaskAttempt }>()
    post.mockReturnValue(inFlight.promise)

    const pending = store.attemptAction('personal', 'ship', ATTEMPT_ID, 'detach')
    inFlight.resolve({ attempt: attempt({ state: 'stopped', live: false }) })

    // A detach whose answer arrived late would filter this workspace's task id out
    // of the new board, and a slug that collides across workspaces means a real row
    // of the board on screen would disappear without anything being deleted.
    expect(await pending).toBeNull()
    expect(store.rows).toHaveLength(0)
    expect(store.error).toBe('')
  })

  it('sends each gesture to the attempt route the server routes', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
    await store.reload('personal')

    for (const action of ['stop', 'resume', 'retry', 'detach'] as const) {
      post.mockResolvedValue({ attempt: attempt(), chat_id: 'chat-9' })
      const answered = await store.attemptAction('personal', 'ship', ATTEMPT_ID, action)
      expect(post).toHaveBeenLastCalledWith(
        `/api/tasks/ship/attempt/${ATTEMPT_ID}/${action}`,
        { workspace: 'personal' },
      )
      expect(answered?.attempt_id).toBe(ATTEMPT_ID)
    }
  })

  it('keeps a refusal in the action slot with the rows it had', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
    await store.reload('personal')
    post.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: {
        error: {
          code: 'task_revision_conflict',
          message: 'The task changed since this delegation was planned; nothing was started.',
          retryable: true,
        },
      },
    }))

    const started = await store.delegate('personal', 'ship', REVISION)

    // The server's own sentence: a stale revision is the record moving on, and
    // re-reading is the only way past it.
    expect(started).toBeNull()
    expect(store.error).toContain('The task changed since this delegation was planned')
    expect(readableTasks(store.rows)).toHaveLength(1)
    expect(store.saving).toBe(false)
  })

  it('returns null rather than a gesture with no attempt to act on', async () => {
    const store = useTaskBoardStore()

    expect(await store.attemptAction('personal', 'ship', '', 'stop')).toBeNull()
    expect(await store.attemptAction('personal', '', ATTEMPT_ID, 'stop')).toBeNull()
    expect(await store.attemptAction('', 'ship', ATTEMPT_ID, 'stop')).toBeNull()
    expect(post).not.toHaveBeenCalled()
    // And a delegation with no revision is not sent at all: the route requires one
    // and a board that cannot name the revision it read must not try.
    expect(await store.delegate('personal', 'ship', '')).toBeNull()
    expect(post).not.toHaveBeenCalled()
  })
})
