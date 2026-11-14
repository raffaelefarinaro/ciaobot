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
