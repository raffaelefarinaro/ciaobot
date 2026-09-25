import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api'
import { useTaskStore } from './tasks'
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
