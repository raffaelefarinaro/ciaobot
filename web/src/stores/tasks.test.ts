// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api'
import { useTaskStore } from './tasks'
import { useProjectStore } from './projects'
import type { Schedule } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

const get = vi.mocked(api.get)

it.each(['move', 'engine'] as const)('excludes conflicting routine writes while %s is pending', async first => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const store = useTaskStore()
  const initial = { schedule_id: 's', workspace: 'personal', model: 'old', provider: 'claude' } as Schedule
  store.schedules = [initial]
  let resolve!: (value: Schedule) => void
  vi.mocked(api.patch).mockReturnValueOnce(new Promise(done => { resolve = done }))
  const owner = first === 'move' ? store.reserveScheduleUpdate('s') : null
  const update = first === 'move'
    ? { workspace: 'work', model: '', provider: '' }
    : { model: 'new', provider: 'claude' as const }
  const pending = store.updateSchedule('s', update, owner ?? undefined)
  expect(store.updatingSchedules.has('s')).toBe(true)
  expect(store.reserveScheduleUpdate('s')).toBeNull()
  await expect(store.updateSchedule('s', { model: 'conflict' })).rejects.toThrow('already being updated')
  expect(api.patch).toHaveBeenCalledTimes(1)
  const result = { ...initial, ...update }
  resolve(result)
  await pending
  if (owner) store.releaseScheduleUpdate('s', owner)
  expect(store.updatingSchedules.has('s')).toBe(false)
  expect(store.schedules[0]).toMatchObject(update)
})

it('does not replace a saved routine with an older pending schedules read', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const store = useTaskStore()
  const old = { schedule_id: 's', model: 'old' } as Schedule
  const saved = { ...old, model: 'new' }
  store.schedules = [old]
  let resolve!: (value: Schedule[]) => void
  get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const read = store.fetchSchedules()
  vi.mocked(api.patch).mockResolvedValueOnce(saved)
  await store.updateSchedule('s', { model: 'new' })
  expect(store.scheduleLoading).toBe(true)
  get.mockResolvedValueOnce([saved, { schedule_id: 'other', model: 'fresh' }])
  resolve([old])
  await read
  expect(store.schedules[0].model).toBe('new')
  expect(store.schedules[1].model).toBe('fresh')
  expect(store.schedulesLoaded).toBe(true)
  expect(get).toHaveBeenCalledTimes(2)
  expect(store.scheduleLoading).toBe(false)
})

it('refreshes all routines after different routine updates overlap a list read', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const store = useTaskStore()
  const a = { schedule_id: 'a', model: 'old-a' } as Schedule
  const b = { schedule_id: 'b', model: 'old-b' } as Schedule
  store.schedules = [a, b]
  let resolveRead!: (value: Schedule[]) => void
  get.mockReturnValueOnce(new Promise(done => { resolveRead = done }))
  const read = store.fetchSchedules()
  vi.mocked(api.patch).mockResolvedValueOnce({ ...a, model: 'new-a' })
    .mockResolvedValueOnce({ ...b, model: 'new-b' })
  await Promise.all([store.updateSchedule('a', { model: 'new-a' }), store.updateSchedule('b', { model: 'new-b' })])
  get.mockResolvedValueOnce([{ ...a, model: 'new-a' }, { ...b, model: 'new-b' }, { schedule_id: 'c', model: 'fresh-c' }])
  resolveRead([a, b])
  await read
  expect(store.schedules.map(s => s.model)).toEqual(['new-a', 'new-b', 'fresh-c'])
  expect(store.schedulesLoaded).toBe(true)
})

it('scopes model reads to the registered active workspace and discards old results', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let resolveFirst!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve }))
  const first = store.fetchModels()
  expect(get).toHaveBeenCalledWith('/api/models?workspace=personal')
  projects.activeWorkspace = 'work'
  expect(store.models).toBeNull()
  resolveFirst({ models: ['old'] })
  await first
  expect(store.models).toBeNull()
})

it('rejects an explicit inactive workspace without invalidating the active request', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.activeWorkspace = 'work'
  const store = useTaskStore()
  let resolve!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const pending = store.fetchModels()
  expect(await store.fetchModels('personal')).toEqual({
    status: 'superseded', workspace: 'personal', generation: null,
  })
  expect(get).toHaveBeenCalledTimes(1)
  expect(store.modelLoading).toBe(true)
  const data = { models: ['work-model'] }
  resolve(data)
  await pending
  expect(store.models).toEqual(data)
})

it('discards a response after a synchronous A to B to A transition', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let resolve!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const pending = store.fetchModels()
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  resolve({ models: ['old'] })
  await pending
  expect(store.models).toBeNull()
  expect(store.modelLoading).toBe(false)
})

it('does not fetch before a registered workspace is ready and encodes the name', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.activeWorkspace = 'Work & life'
  const store = useTaskStore()
  await store.fetchModels()
  expect(get).not.toHaveBeenCalled()
  projects.workspaces = [{ name: 'Work & life' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  get.mockResolvedValueOnce({ models: [] })
  await store.fetchModels()
  expect(get).toHaveBeenCalledWith('/api/models?workspace=Work+%26+life')
})

it('reports a current failure without rejecting and clears it on successful retry', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  get.mockRejectedValueOnce(new Error('offline'))
  await expect(store.fetchModels()).resolves.toMatchObject({ status: 'error', workspace: 'personal' })
  expect(store.modelError).toBe('offline')
  expect(store.modelResult).toBe('error')
  expect(store.modelLoading).toBe(false)
  get.mockResolvedValueOnce({ models: [] })
  await store.fetchModels()
  expect(store.modelError).toBe('')
  expect(store.modelResult).toBe('success')
  expect(store.models).toEqual({ models: [] })
})

it('returns a per-call outcome and retains the same workspace snapshot as stale on failure', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  get.mockResolvedValueOnce({ models: ['saved'] })
  expect(await store.fetchModels()).toMatchObject({ status: 'success', workspace: 'personal' })
  get.mockRejectedValueOnce(new Error('offline'))
  expect(await store.fetchModels('personal')).toMatchObject({ status: 'error', workspace: 'personal' })
  expect(store.models).toEqual({ models: ['saved'] })
  expect(store.modelStale).toBe(true)
  projects.activeWorkspace = 'work'
  expect(store.models).toBeNull()
  expect(store.modelStale).toBe(false)
})

it('returns superseded for an old result, even after A to B to A', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let resolve!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const old = store.fetchModels()
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  resolve({ models: ['old'] })
  expect(await old).toMatchObject({ status: 'superseded', workspace: 'personal' })
  expect(store.models).toBeNull()
})

it('returns superseded for an older successful reread and leaves the newer result intact', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let firstResolve!: (value: unknown) => void
  let secondResolve!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise(resolve => { firstResolve = resolve }))
  get.mockReturnValueOnce(new Promise(resolve => { secondResolve = resolve }))
  const first = store.fetchModels()
  const second = store.fetchModels()
  secondResolve({ models: ['new'] })
  expect(await second).toMatchObject({ status: 'success', workspace: 'personal', generation: 2 })
  firstResolve({ models: ['old'] })
  expect(await first).toMatchObject({ status: 'superseded', workspace: 'personal', generation: 1 })
  expect(store.models).toEqual({ models: ['new'] })
  expect(store.modelStale).toBe(false)
})

it('does not let an older same-workspace failure clear a newer pending read', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let reject!: (reason: Error) => void
  let resolve!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
  get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const old = store.fetchModels()
  const current = store.fetchModels()
  reject(new Error('old error'))
  expect(await old).toMatchObject({ status: 'superseded', workspace: 'personal' })
  expect(store.modelLoading).toBe(true)
  expect(store.modelError).toBe('')
  resolve({ models: ['new'] })
  await current
  expect(store.models).toEqual({ models: ['new'] })
  expect(store.modelResult).toBe('success')
})

it('invalidates in-flight data when registry membership is removed and recovers on restoration', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let reject!: (reason: Error) => void
  get.mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
  const pending = store.fetchModels()
  projects.workspaces = []
  expect(store.modelWorkspaceEligible).toBe(false)
  expect(store.modelLoading).toBe(false)
  reject(new Error('stale failure'))
  await pending
  expect(store.modelError).toBe('')
  await store.fetchModels()
  expect(get).toHaveBeenCalledTimes(1)
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  get.mockResolvedValueOnce({ models: ['fresh'] })
  await store.fetchModels()
  expect(store.models).toEqual({ models: ['fresh'] })
})

it('keeps schedule failures observable through fetchAll while model failures do not reject', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  get.mockImplementation(async url => {
    if (url === '/api/schedules') throw new Error('schedule offline')
    if (url.startsWith('/api/models')) throw new Error('models offline')
    return {}
  })
  await expect(store.fetchAll()).rejects.toThrow('schedule offline')
  expect(store.scheduleLoadError).toBe('schedule offline')
  expect(store.modelError).toBe('models offline')
  expect(store.loading).toBe(false)
})

it('does not retain the disposed store watcher when a task store is recreated', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const old = useTaskStore()
  old.$dispose()
  const next = useTaskStore()
  const oldTransition = old.modelTransition
  projects.activeWorkspace = 'work'
  expect(old.modelTransition).toBe(oldTransition)
  expect(next.modelTransition).toBe(1)
})

it('does not report an in-flight model read as successful after store disposal', async () => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const store = useTaskStore()
  let resolve!: (value: unknown) => void
  get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const pending = store.fetchModels()
  store.$dispose()
  resolve({ models: ['old'] })
  expect(await pending).toMatchObject({ status: 'superseded', workspace: 'personal' })
})

function schedule(id: string): Schedule {
  return {
    schedule_id: id,
    prompt: `Run ${id}`,
    frequency: 'manual',
    enabled: true,
  } as Schedule
}

describe('task store schedule loading', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('retries again if an update completes during the retry', async () => {
    const store = useTaskStore()
    let first!: (value: Schedule[]) => void
    let second!: (value: Schedule[]) => void
    get.mockReturnValueOnce(new Promise(resolve => { first = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { second = resolve }))
      .mockResolvedValueOnce([schedule('fresh')])
    vi.mocked(api.patch).mockResolvedValueOnce(schedule('a')).mockResolvedValueOnce(schedule('b'))
    const read = store.fetchSchedules()
    await store.updateSchedule('a', { model: 'a' })
    first([schedule('old')])
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    await store.updateSchedule('b', { model: 'b' })
    second([schedule('still-old')])
    await read
    expect(get).toHaveBeenCalledTimes(3)
    expect(store.schedules.map(s => s.schedule_id)).toEqual(['fresh'])
    expect(store.scheduleLoading).toBe(false)
  })

  it('reports retry failure to the original caller and clears loading', async () => {
    const store = useTaskStore()
    let first!: (value: Schedule[]) => void
    get.mockReturnValueOnce(new Promise(resolve => { first = resolve }))
      .mockRejectedValueOnce(new Error('retry offline'))
    vi.mocked(api.patch).mockResolvedValueOnce(schedule('a'))
    const read = store.fetchSchedules()
    await store.updateSchedule('a', { model: 'new' })
    first([schedule('old')])
    await expect(read).rejects.toThrow('retry offline')
    expect(store.scheduleLoadError).toBe('retry offline')
    expect(store.scheduleLoading).toBe(false)
  })

  it.each(['success', 'failure'] as const)('joins a newer %s when it supersedes a retry', async outcome => {
    const store = useTaskStore()
    let first!: (value: Schedule[]) => void
    let retry!: (value: Schedule[]) => void
    let winnerResolve!: (value: Schedule[]) => void
    let winnerReject!: (error: Error) => void
    get.mockReturnValueOnce(new Promise(resolve => { first = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { retry = resolve }))
      .mockReturnValueOnce(new Promise((resolve, reject) => { winnerResolve = resolve; winnerReject = reject }))
    vi.mocked(api.patch).mockResolvedValueOnce(schedule('a'))
    const older = store.fetchSchedules()
    await store.updateSchedule('a', { model: 'new' })
    first([schedule('old')])
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    const newer = store.fetchSchedules()
    let olderSettled = false
    void older.then(() => { olderSettled = true }, () => { olderSettled = true })
    await Promise.resolve()
    expect(olderSettled).toBe(false)
    if (outcome === 'success') {
      winnerResolve([schedule('winner')])
      await Promise.all([older, newer])
      expect(store.schedules.map(s => s.schedule_id)).toEqual(['winner'])
    } else {
      const both = Promise.allSettled([older, newer])
      winnerReject(new Error('winner offline'))
      const results = await both
      expect(results.map(r => r.status)).toEqual(['rejected', 'rejected'])
      expect(store.scheduleLoadError).toBe('winner offline')
    }
    expect(store.scheduleLoading).toBe(false)
    retry([schedule('stale')])
    await Promise.resolve()
    expect(store.schedules.some(s => s.schedule_id === 'stale')).toBe(false)
  })

  it('joins the last of three overlapping reads even when an old read rejects', async () => {
    const store = useTaskStore()
    let rejectOld!: (error: Error) => void
    let resolveLast!: (value: Schedule[]) => void
    get.mockReturnValueOnce(new Promise((_, reject) => { rejectOld = reject }))
      .mockReturnValueOnce(new Promise(() => {}))
      .mockReturnValueOnce(new Promise(resolve => { resolveLast = resolve }))
    const first = store.fetchSchedules()
    const second = store.fetchSchedules()
    const third = store.fetchSchedules()
    rejectOld(new Error('obsolete failure'))
    resolveLast([schedule('last')])
    await Promise.all([first, second, third])
    expect(store.schedules.map(s => s.schedule_id)).toEqual(['last'])
    expect(store.scheduleLoadError).toBe('')
    expect(store.scheduleLoading).toBe(false)
  })

  it('keeps the newest concurrent schedule response', async () => {
    let resolveFirst!: (value: Schedule[]) => void
    let resolveSecond!: (value: Schedule[]) => void
    get
      .mockReturnValueOnce(new Promise<Schedule[]>(resolve => { resolveFirst = resolve }))
      .mockReturnValueOnce(new Promise<Schedule[]>(resolve => { resolveSecond = resolve }))
    const store = useTaskStore()

    const first = store.fetchSchedules()
    const second = store.fetchSchedules()
    expect(get).toHaveBeenCalledTimes(2)
    expect(store.scheduleLoading).toBe(true)
    expect(store.schedulesLoaded).toBe(false)

    resolveSecond([schedule('two')])
    await second
    expect(store.schedulesLoaded).toBe(true)
    expect(store.schedules.map(item => item.schedule_id)).toEqual(['two'])

    resolveFirst([schedule('one')])
    await first
    expect(store.scheduleLoading).toBe(false)
    expect(store.scheduleLoadError).toBe('')
    expect(store.schedules.map(item => item.schedule_id)).toEqual(['two'])
  })

  it('preserves the last snapshot when a refresh fails and clears the error on retry', async () => {
    get.mockResolvedValueOnce([schedule('one')])
    const store = useTaskStore()
    await store.fetchSchedules()

    get.mockRejectedValueOnce(new Error('offline'))
    await expect(store.fetchSchedules()).rejects.toThrow('offline')
    expect(store.scheduleLoadError).toBe('offline')
    expect(store.schedulesLoaded).toBe(true)
    expect(store.schedules.map(item => item.schedule_id)).toEqual(['one'])

    get.mockResolvedValueOnce([schedule('two')])
    await store.fetchSchedules()
    expect(store.scheduleLoadError).toBe('')
    expect(store.schedules.map(item => item.schedule_id)).toEqual(['two'])
  })
})
