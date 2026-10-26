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
    chat_id: '',
    attempt_id: '',
    linked_chats: [],
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
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

/** A write's answer: the record as stored, description included. */
function stored(overrides: Partial<TaskDetail> = {}): { task: TaskDetail } {
  const detail = {
    ...task(),
    revision: NEXT_REVISION,
    body: '',
    resolution: '',
    completions: [],
    delegation_log: '',
    updates: [] as TaskDetail['updates'],
    ...overrides,
  }
  return { task: { ...detail, updates: detail.updates ?? [] } }
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

  describe('decision reasons (#1730)', () => {
    it('remove sends the reason only when there is one', async () => {
      const store = useTaskBoardStore()
      del.mockResolvedValue({ workspace: 'personal', deleted: 'ship' })
      await store.remove('personal', 'ship', REVISION, 'not mine')
      expect(del).toHaveBeenLastCalledWith('/api/tasks/ship', {
        workspace: 'personal', expected_revision: REVISION, reason: 'not mine',
      })
      await store.remove('personal', 'ship', REVISION)
      expect(del).toHaveBeenLastCalledWith('/api/tasks/ship', {
        workspace: 'personal', expected_revision: REVISION,
      })
      await store.remove('personal', 'ship', REVISION, '   ')
      expect(del).toHaveBeenLastCalledWith('/api/tasks/ship', {
        workspace: 'personal', expected_revision: REVISION,
      })
    })

    it('complete sends the reason only when there is one', async () => {
      const store = useTaskBoardStore()
      post.mockResolvedValue(stored({ status: 'done' }))
      await store.complete('personal', 'ship', REVISION, 'note', ' good as is ')
      expect(post).toHaveBeenLastCalledWith('/api/tasks/ship/complete', {
        workspace: 'personal', expected_revision: REVISION, resolution: 'note', reason: 'good as is',
      })
      await store.complete('personal', 'ship', REVISION, 'note')
      expect(post).toHaveBeenLastCalledWith('/api/tasks/ship/complete', {
        workspace: 'personal', expected_revision: REVISION, resolution: 'note',
      })
      await store.complete('personal', 'ship', REVISION, 'note', '\n ')
      expect(post).toHaveBeenLastCalledWith('/api/tasks/ship/complete', {
        workspace: 'personal', expected_revision: REVISION, resolution: 'note',
      })
    })

    it('attemptAction sends the reason only when there is one', async () => {
      const store = useTaskBoardStore()
      post.mockResolvedValue({ attempt: attempt() })
      await store.attemptAction('personal', 'ship', ATTEMPT_ID, 'detach', 'not a task')
      expect(post).toHaveBeenLastCalledWith(
        `/api/tasks/ship/attempt/${ATTEMPT_ID}/detach`,
        { workspace: 'personal', reason: 'not a task' },
      )
      await store.attemptAction('personal', 'ship', ATTEMPT_ID, 'detach', '  ')
      expect(post).toHaveBeenLastCalledWith(
        `/api/tasks/ship/attempt/${ATTEMPT_ID}/detach`,
        { workspace: 'personal' },
      )
    })
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
      outcome: '',
      summary: '',
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
      completed_at: null,
      has_resolution: false,
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

  it('sends hand-over instructions trimmed, and omits blank ones', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue({ workspace: 'personal', tasks: [task()] })
    await store.reload('personal')
    post.mockResolvedValue(delegated())

    await store.delegate('personal', 'ship', REVISION, undefined, '  Done = a PR link.\n')
    expect(post).toHaveBeenLastCalledWith('/api/tasks/ship/delegate', {
      workspace: 'personal',
      expected_revision: REVISION,
      instructions: 'Done = a PR link.',
    })

    await store.delegate('personal', 'ship', REVISION, undefined, '   ')
    expect(post).toHaveBeenLastCalledWith('/api/tasks/ship/delegate', {
      workspace: 'personal',
      expected_revision: REVISION,
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

function orderOf(version: number, backlog: string[] = ['ship'], in_progress: string[] = []) {
  return { version, sequences: { backlog, in_progress, in_review: [], done: [] } }
}

function board(version: number, extra: Record<string, unknown> = {}) {
  return { workspace: 'personal', tasks: [task()], order: orderOf(version), order_error: null, ...extra }
}

function refusal(code: string, message = 'refused') {
  return Object.assign(new Error('HTTP 409'), { payload: { error: { code, message } } })
}

describe('taskBoard store: card order', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  async function loaded(version = 3) {
    const store = useTaskBoardStore()
    get.mockResolvedValue(board(version))
    await store.reload('personal')
    return store
  }

  it('reload stores the server order and resets it on a workspace switch', async () => {
    const store = await loaded(3)
    expect(store.order.version).toBe(3)
    expect(store.order.sequences.backlog).toEqual(['ship'])

    get.mockResolvedValue({ workspace: 'work', tasks: [] })
    await store.reload('work')

    expect(store.order.version).toBe(0)
    expect(store.order.sequences.backlog).toEqual([])
  })

  it('update with placement sends placement and expected_order_version and adopts the returned order', async () => {
    const store = await loaded(3)
    patch.mockResolvedValue({ ...stored({ status: 'in_progress' }), order: orderOf(4, [], ['ship']), order_error: null })

    const saved = await store.update('personal', 'ship', REVISION, { status: 'in_progress' }, undefined, {
      before_id: null,
      after_id: 'other',
    })

    expect(saved?.status).toBe('in_progress')
    expect(patch.mock.calls[0]![1]).toMatchObject({
      status: 'in_progress',
      placement: { before_id: null, after_id: 'other', expected_order_version: 3 },
    })
    expect(store.order.version).toBe(4)
    expect(store.order.sequences.in_progress).toEqual(['ship'])
  })

  it('does not send a placement on an update that keeps the status', async () => {
    const store = await loaded(3)
    patch.mockResolvedValue(stored({ title: 'Renamed' }))

    await store.update('personal', 'ship', REVISION, { title: 'Renamed' }, undefined, { before_id: 'x', after_id: null })

    expect(patch.mock.calls[0]![1]).not.toHaveProperty('placement')
  })

  it('complete with placement posts placement along with the resolution in one request', async () => {
    const store = await loaded(3)
    post.mockResolvedValue({ ...stored({ status: 'done' }), order: orderOf(4, [], []), order_error: null })

    await store.complete('personal', 'ship', REVISION, 'Shipped', undefined, { before_id: 'x', after_id: null })

    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0]![0]).toBe('/api/tasks/ship/complete')
    expect(post.mock.calls[0]![1]).toMatchObject({
      resolution: 'Shipped',
      placement: { before_id: 'x', after_id: null, expected_order_version: 3 },
    })
    expect(store.order.version).toBe(4)
  })

  it('reorder posts to /order with the held version, then adopts order and the row', async () => {
    const store = await loaded(3)
    post.mockResolvedValue({ ...stored({ title: 'Moved' }), order: orderOf(4, ['b', 'ship']), order_error: null })

    const result = await store.reorder('personal', 'ship', { before_id: null, after_id: 'b' }, 3)

    expect(result).toEqual({ ok: true })
    expect(post).toHaveBeenCalledWith('/api/tasks/ship/order', {
      workspace: 'personal',
      before_id: null,
      after_id: 'b',
      expected_order_version: 3,
    })
    expect(store.order.sequences.backlog).toEqual(['b', 'ship'])
    expect(readableTasks(store.rows)[0]!.revision).toBe(NEXT_REVISION)
  })

  it('reorder conflict reloads and reports a retry message without replaying', async () => {
    const store = await loaded(3)
    post.mockRejectedValue(refusal('task_order_conflict'))
    get.mockResolvedValue(board(5))

    const result = await store.reorder('personal', 'ship', { before_id: null, after_id: 'b' }, 3)

    expect(result).toMatchObject({ ok: false, conflict: true })
    expect(store.error).toBe('The board changed while you were moving that. It has been refreshed; try again.')
    expect(post).toHaveBeenCalledTimes(1)
    expect(store.order.version).toBe(5)
  })

  it('reorder failure restores authoritative state (reload) and keeps the plain-language error', async () => {
    const store = await loaded(3)
    post.mockRejectedValue(refusal('task_order_invalid', 'That card is not in this column.'))
    get.mockClear()
    get.mockResolvedValue(board(3))

    const result = await store.reorder('personal', 'ship', { before_id: 'zzz', after_id: null }, 3)

    expect(result).toEqual({ ok: false, conflict: false, message: 'That card is not in this column.' })
    expect(store.error).toBe('That card is not in this column.')
    expect(get).toHaveBeenCalledTimes(1)
    expect(store.order.version).toBe(3)
  })

  it('late order response from an older request is ignored', async () => {
    const store = await loaded(3)
    const slow = deferred<unknown>()
    post.mockReturnValueOnce(slow.promise as never)
    const pending = store.reorder('personal', 'ship', { before_id: null, after_id: null }, 3)
    await Promise.resolve()
    // A newer read lands first and takes the newest ticket.
    get.mockResolvedValue(board(6))
    await store.reload('personal')
    slow.resolve({ ...stored(), order: orderOf(4), order_error: null })
    await pending
    expect(store.order.version).toBe(6)

    // A lower version from a request that still holds the newest ticket is dropped too.
    patch.mockResolvedValue({ ...stored({ title: 'Renamed' }), order: orderOf(2), order_error: null })
    await store.update('personal', 'ship', REVISION, { title: 'Renamed' })
    expect(store.order.version).toBe(6)
  })

  it('writes are serialised: a second reorder waits for the first', async () => {
    const store = await loaded(3)
    const first = deferred<unknown>()
    post.mockReturnValueOnce(first.promise as never)
    post.mockResolvedValueOnce({ ...stored(), order: orderOf(5), order_error: null })

    const a = store.reorder('personal', 'ship', { before_id: null, after_id: 'b' }, 3)
    const b = store.reorder('personal', 'ship', { before_id: null, after_id: 'c' }, 4)
    await Promise.resolve()
    await Promise.resolve()
    expect(post).toHaveBeenCalledTimes(1)

    first.resolve({ ...stored(), order: orderOf(4), order_error: null })
    expect(await a).toEqual({ ok: true })
    expect(await b).toEqual({ ok: true })
    expect(post).toHaveBeenCalledTimes(2)
    expect(store.order.version).toBe(5)
  })

  it('a write queued behind a conflict is dropped without a request, not replayed', async () => {
    const store = await loaded(3)
    let reject!: (e: unknown) => void
    post.mockReturnValueOnce(new Promise((_, r) => { reject = r }) as never)
    get.mockResolvedValue(board(5))

    const a = store.reorder('personal', 'ship', { before_id: null, after_id: 'b' }, 3)
    const b = store.reorder('personal', 'ship', { before_id: null, after_id: 'c' }, 3)
    reject(refusal('task_order_conflict'))

    expect(await a).toMatchObject({ ok: false, conflict: true })
    expect(await b).toMatchObject({ ok: false, conflict: true })
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('order response from a workspace the pane left is dropped', async () => {
    const store = await loaded(3)
    const slow = deferred<unknown>()
    post.mockReturnValueOnce(slow.promise as never)
    const pending = store.reorder('personal', 'ship', { before_id: null, after_id: null }, 3)
    await Promise.resolve()
    get.mockResolvedValue({ workspace: 'work', tasks: [], order: orderOf(9, []), order_error: null })
    await store.reload('work')

    slow.resolve({ ...stored(), order: orderOf(4, ['ship']), order_error: null })
    await pending

    expect(store.order.version).toBe(9)
    expect(store.rows).toHaveLength(0)
  })

  it("remoteChangeEpoch increments for an external change and not for the store's own write reload", async () => {
    const store = await loaded(3)
    expect(store.remoteChangeEpoch).toBe(0)

    get.mockResolvedValue(board(4))
    await store.reload('personal')
    expect(store.remoteChangeEpoch).toBe(1)

    get.mockResolvedValue(board(4))
    await store.reload('personal')
    expect(store.remoteChangeEpoch).toBe(1)

    post.mockRejectedValue(refusal('task_order_conflict'))
    get.mockResolvedValue(board(7))
    await store.reorder('personal', 'ship', { before_id: null, after_id: null }, 4)
    expect(store.order.version).toBe(7)
    expect(store.remoteChangeEpoch).toBe(1)
  })

  it('rejects order writes locally while the order file is unreadable', async () => {
    const store = useTaskBoardStore()
    get.mockResolvedValue(board(-1, { order_error: 'task-order.json is not valid JSON' }))
    await store.reload('personal')
    expect(store.orderError).toBe('task-order.json is not valid JSON')

    const moved = await store.reorder('personal', 'ship', { before_id: null, after_id: null }, -1)
    const updated = await store.update('personal', 'ship', REVISION, { status: 'in_progress' }, undefined, {
      before_id: null,
      after_id: null,
    })
    const completed = await store.complete('personal', 'ship', REVISION, undefined, undefined, {
      before_id: null,
      after_id: null,
    })

    expect(moved).toEqual({ ok: false, conflict: false, message: 'task-order.json is not valid JSON' })
    expect(updated).toBeNull()
    expect(completed).toBeNull()
    expect(store.error).toBe('task-order.json is not valid JSON')
    expect(post).not.toHaveBeenCalled()
    expect(patch).not.toHaveBeenCalled()
  })
})
