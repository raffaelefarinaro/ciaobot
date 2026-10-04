// @vitest-environment jsdom

/**
 * The import store's run wiring (#1041, C8).
 *
 * Two guarantees are under test. The first is the wire: what the run, cancel and
 * remove paths send, and what they refuse to send. `POST .../run` takes the
 * workspace and nothing else — no model, no provider, no selection — because
 * which model reads a user's own history is the configuration's answer and a
 * request field would be a client choosing it. The remove path takes the
 * workspace in the query, which is where the route reads it.
 *
 * The second is the guard: a batch id, a status and a set of provenance rows all
 * name one workspace's private store, and the `1`–`9` shortcuts can move the pane
 * to another workspace while any of these requests is open. Adopting such an
 * answer would draw another workspace's batch under the new name, and the next
 * press on that row would present its id to a store that has never heard of it.
 * The same hold applies to a stale read: a list re-read that was issued before a
 * run answered must not put `queued` back on the screen.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
import { api } from '../lib/api'
import { useImportStore } from './import'

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)
const del = vi.mocked(api.del)

function batch(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    batch_id: 'batch-1',
    workspace: 'personal',
    destination: '/tmp/p/memory-vault/personal',
    status: 'queued',
    extraction_revision: 1,
    sources: [{ provider: 'claude_code', source_id: 'sess-a', content_digest: '', status: 'pending' }],
    progress: { total_sources: 1, completed_sources: 0, proposals_filed: 0, skipped: 0, current_source_id: '' },
    provenance: [],
    created_at: '2026-10-04T09:00:00+00:00',
    updated_at: '2026-10-04T09:00:00+00:00',
    error: '',
    ...over,
  }
}

/** A promise the test resolves by hand, so a request can be held in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void } {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('the import store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    get.mockReset()
    post.mockReset()
    del.mockReset()
  })

  it('reads the list and the retention window the server reports', async () => {
    get.mockResolvedValue({ batches: [batch()], retention_days: 30 })
    const store = useImportStore()

    await store.reload('personal')

    expect(get).toHaveBeenCalledWith('/api/import/batches?workspace=personal')
    expect(store.batches).toHaveLength(1)
    expect(store.batches[0]!.batch_id).toBe('batch-1')
    // The window is the engine's own number. Nothing here spells it out, so the
    // sentence in the panel cannot drift from the sweep that prunes on it.
    expect(store.retentionDays).toBe(30)
    expect(store.loaded).toBe(true)
  })

  it('files a batch over ids only, never a path', async () => {
    get.mockResolvedValue({ batches: [], retention_days: 30 })
    post.mockResolvedValue({ batch: batch() })
    const store = useImportStore()
    await store.reload('personal')

    const filed = await store.file('personal', [{ provider: 'claude_code', source_id: 'sess-a' }])

    expect(post).toHaveBeenCalledWith('/api/import/batches', {
      workspace: 'personal',
      sources: [{ provider: 'claude_code', source_id: 'sess-a' }],
    })
    expect(filed?.batch_id).toBe('batch-1')
    // Adopted onto the list, so the run it can be started from is on screen.
    expect(store.batches.map((row) => row.batch_id)).toEqual(['batch-1'])
  })

  it('says so when a 2xx carries no record, rather than reading as nothing happened', async () => {
    get.mockResolvedValue({ batches: [], retention_days: 30 })
    post.mockResolvedValue({ batch: undefined })
    const store = useImportStore()
    await store.reload('personal')

    expect(await store.file('personal', [{ provider: 'claude_code', source_id: 'sess-a' }])).toBeNull()
    expect(store.error).toContain('did not answer with it')
  })

  it('starts a run with the workspace and nothing else', async () => {
    get.mockResolvedValue({ batches: [batch()], retention_days: 30 })
    post.mockResolvedValue({ batch: batch({ status: 'running', updated_at: '2026-10-04T09:01:00+00:00' }) })
    const store = useImportStore()
    await store.reload('personal')

    const started = await store.start('personal', 'batch-1')

    // No model, no provider, no selection: the run resolves those from
    // configuration, and the route ignores a body that names them.
    expect(post).toHaveBeenCalledWith('/api/import/batches/batch-1/run', { workspace: 'personal' })
    expect(started?.status).toBe('running')
    expect(store.batches[0]!.status).toBe('running')
  })

  it('takes the workspace in the query when a record is removed', async () => {
    get.mockResolvedValue({ batches: [batch({ status: 'done' })], retention_days: 30 })
    del.mockResolvedValue(undefined)
    const store = useImportStore()
    await store.reload('personal')

    expect(await store.forget('personal', 'batch-1')).toBe(true)
    expect(del).toHaveBeenCalledWith('/api/import/batches/batch-1?workspace=personal')
    expect(store.batches).toHaveLength(0)
    expect(store.error).toBe('')
  })

  it('never adopts a run that answers after the workspace moved', async () => {
    get.mockImplementation(async (path: string) => (
      String(path).includes('workspace=personal')
        ? { batches: [batch()], retention_days: 30 }
        : { batches: [], retention_days: 30 }
    ))
    const held = deferred<{ batch: Record<string, unknown> }>()
    post.mockReturnValue(held.promise)
    const store = useImportStore()
    await store.reload('personal')

    const started = store.start('personal', 'batch-1')
    // The `1`–`9` shortcuts can move the pane while the run is in flight.
    await store.reload('work')
    held.resolve({ batch: batch({ status: 'running', updated_at: '2026-10-04T09:01:00+00:00' }) })

    expect(await started).toBeNull()
    expect(store.batches).toEqual([])
    expect(store.batches.some((row) => row.batch_id === 'batch-1')).toBe(false)
  })

  it('never puts queued back on a row a run already moved to running', async () => {
    // The stale read: issued before the run answered, and it is the read that
    // says the batch is still queued.
    const stale = deferred<{ batches: Record<string, unknown>[]; retention_days: number }>()
    get.mockResolvedValueOnce({ batches: [batch()], retention_days: 30 })
    get.mockReturnValueOnce(stale.promise)
    post.mockResolvedValue({ batch: batch({ status: 'running', updated_at: '2026-10-04T09:01:00+00:00' }) })
    const store = useImportStore()
    await store.reload('personal')

    const reading = store.reload('personal')
    await store.start('personal', 'batch-1')
    stale.resolve({ batches: [batch()], retention_days: 30 })
    await reading

    expect(store.batches[0]!.status).toBe('running')
  })

  it('keeps the rows and marks them stale when a refresh fails', async () => {
    get.mockResolvedValueOnce({ batches: [batch()], retention_days: 30 })
    get.mockRejectedValueOnce(new Error('the engine went away'))
    const store = useImportStore()
    await store.reload('personal')
    await store.reload('personal')

    expect(store.batches).toHaveLength(1)
    expect(store.loadError).toContain('the engine went away')
    expect(store.loaded).toBe(true)
  })

  it('refuses a record with no id, rather than drawing a row for a batch that is not there', async () => {
    get.mockResolvedValue({ batches: [{ status: 'done', progress: {} }], retention_days: 30 })
    const store = useImportStore()

    await store.reload('personal')

    expect(store.batches).toEqual([])
  })

  it('reads a status it does not know as queued, and keeps a list read failure apart from an action failure', async () => {
    get.mockResolvedValueOnce({ batches: [batch({ status: 'from-a-newer-engine' })], retention_days: 30 })
    get.mockRejectedValueOnce(new Error('the engine went away'))
    post.mockRejectedValueOnce(new Error('another import is still open'))
    const store = useImportStore()
    await store.reload('personal')
    await store.reload('personal')

    expect(store.batches[0]!.status).toBe('queued')

    await store.cancel('personal', 'batch-1')
    expect(store.error).toContain('another import is still open')
    // A failed list read is not the outcome of an action: the action's own
    // message must survive it, and the other way round.
    expect(store.loadError).toContain('the engine went away')
    store.clearError()
    expect(store.error).toBe('')
    expect(store.loadError).toContain('the engine went away')
  })
})