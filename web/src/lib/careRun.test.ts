import { describe, expect, it, vi } from 'vitest'
import { careRunCountsLine, careRunPath, fetchCareRun, isCareSchedule } from './careRun'

describe('isCareSchedule', () => {
  it('matches the care routine and its per-workspace copies', () => {
    expect(isCareSchedule('system-memory-curation')).toBe(true)
    expect(isCareSchedule('system-memory-curation@personal')).toBe(true)
  })

  it('does not match other automations', () => {
    expect(isCareSchedule('system-memory-curation-extra')).toBe(false)
    expect(isCareSchedule('sched-1')).toBe(false)
    expect(isCareSchedule('')).toBe(false)
  })
})

describe('careRunPath', () => {
  it('addresses the schedule-scoped read-only route', () => {
    expect(careRunPath('system-memory-curation@personal')).toBe(
      '/api/schedules/system-memory-curation%40personal/care-run',
    )
  })
})

describe('careRunCountsLine', () => {
  it('names every derived count in a fixed order', () => {
    expect(careRunCountsLine({
      run_id: 'r', planned: 4, completed: 3, deferred: 1, failed: 0, counts_complete: true, reasons: [],
    })).toBe('Planned 4 · completed 3 · deferred 1 · failed 0')
  })
})

describe('fetchCareRun', () => {
  it('returns the outcome the route sent, or null when there is none', async () => {
    const get = vi.fn(async () => ({ care_run: { run_id: 'r', planned: 1, completed: 1, deferred: 0, failed: 0, counts_complete: true, reasons: [] } }))
    await expect(fetchCareRun('system-memory-curation', get)).resolves.toMatchObject({ planned: 1 })
    expect(get).toHaveBeenCalledWith('/api/schedules/system-memory-curation/care-run')

    const none = vi.fn(async () => ({ care_run: null }))
    await expect(fetchCareRun('system-memory-curation', none)).resolves.toBeNull()
  })

  it('refuses a response that does not carry the care_run field', async () => {
    const get = vi.fn(async () => ({ something: 'else' }))
    await expect(fetchCareRun('system-memory-curation', get)).rejects.toThrow(/unexpected/)
  })
})
