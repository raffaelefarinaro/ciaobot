import { describe, expect, it, vi } from 'vitest'
import {
  careRunCountsLine,
  careRunPath,
  fetchCareRun,
  fetchCareRunReport,
  isCareSchedule,
  isNoOp,
  outstandingGroups,
  reviewLink,
  settledLine,
  weeklyLine,
} from './careRun'

describe('isCareSchedule', () => {
  it('matches the care routine and its per-workspace copies', () => {
    expect(isCareSchedule('system-memory-curation')).toBe(true)
    expect(isCareSchedule('system-memory-curation@personal')).toBe(true)
  })

  it('does not match other automations', () => {
    expect(isCareSchedule('system-memory-curation-extra')).toBe(false)
    expect(isCareSchedule('sched-1')).toBe(false)
    expect(isCareSchedule('system-memory-curation@')).toBe(false)
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
    expect(careRunCountsLine({ planned: 4, completed: 3, deferred: 1, failed: 0 })).toBe('Planned 4 · completed 3 · deferred 1 · failed 0')
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

const item = (status: string) => ({ item_key: 'k', pass_id: 'p', status, detail: '', receipt_ids: [] })
const COUNTS = { planned: 1, completed: 1, deferred: 0, failed: 0 }

describe('fetchCareRunReport', () => {
  it('asks for the exact dispatch, encoded', async () => {
    const get = vi.fn().mockResolvedValue({ state: 'pending', care_run: null, report_available: false })
    await fetchCareRunReport('system-memory-curation@a', 'd/1 x', get)
    expect(get).toHaveBeenCalledWith('/api/schedules/system-memory-curation%40a/care-run?dispatch_id=d%2F1%20x')
  })

  it('passes every non-ok state through', async () => {
    for (const state of ['pending', 'superseded', 'ambiguous', 'unavailable']) {
      const get = vi.fn().mockResolvedValue({ state, care_run: null, report_available: false })
      await expect(fetchCareRunReport('s', 'd', get)).resolves.toMatchObject({ state })
    }
  })

  it('accepts an ok answer with counts', async () => {
    const get = vi.fn().mockResolvedValue({ state: 'ok', care_run: null, report_available: false, job_status: 'ok', counts: COUNTS })
    await expect(fetchCareRunReport('s', 'd', get)).resolves.toMatchObject({ counts: COUNTS })
  })

  it.each([
    ['no state', { report_available: true }],
    ['unknown state', { state: 'done', report_available: true }],
    ['no report_available', { state: 'pending' }],
    ['ok without counts', { state: 'ok', report_available: true, job_status: 'ok' }],
    ['ok with unknown job status', { state: 'ok', report_available: true, job_status: 'weird', counts: COUNTS }],
    ['ok with a bad outcome', { state: 'ok', report_available: true, job_status: 'ok', counts: COUNTS, outcomes: [{ status: 1 }] }],
    ['ok with a bad weekly', { state: 'ok', report_available: true, job_status: 'ok', counts: COUNTS, weekly: {} }],
    ['an array', []],
    ['null', null],
  ])('rejects %s', async (_name, body) => {
    await expect(fetchCareRunReport('s', 'd', vi.fn().mockResolvedValue(body))).rejects.toThrow(/unexpected/)
  })
})

describe('outstandingGroups', () => {
  it('orders reasons failures first and leaves settled items out', () => {
    const groups = outstandingGroups(['applied', 'queued', 'blocked', 'failed', 'no_change', 'failed'].map(item))
    expect(groups.map(g => [g.status, g.items.length])).toEqual([['failed', 2], ['blocked', 1], ['queued', 1]])
  })
})

describe('settledLine and isNoOp', () => {
  it('tallies settled statuses only', () => {
    expect(settledLine(['applied', 'applied', 'no_change', 'failed'].map(item))).toBe('applied 2 · no change needed 1')
    expect(settledLine([])).toBe('')
  })

  it('is a no-op only when there are items and all report no change', () => {
    expect(isNoOp([])).toBe(false)
    expect(isNoOp(['no_change', 'no_change'].map(item))).toBe(true)
    expect(isNoOp(['no_change', 'queued'].map(item))).toBe(false)
  })
})

describe('weeklyLine', () => {
  it('names both marker states and shows an unknown one verbatim', () => {
    expect(weeklyLine('advanced')).toContain('advanced')
    expect(weeklyLine('not_advanced')).toContain('did not advance')
    expect(weeklyLine('odd')).toBe('Weekly marker: odd')
  })
})

describe('reviewLink', () => {
  const key = `prv_${'b'.repeat(64)}`
  it('builds the canonical workspace-scoped proposal link', () => {
    expect(reviewLink('personal', key)).toBe(`/memory/review?show=suggested&workspace=personal&proposal=${key}`)
  })
  it('refuses a missing workspace or a key that is not a proposal key', () => {
    expect(reviewLink('', key)).toBeNull()
    expect(reviewLink('personal', 'prv_short')).toBeNull()
    expect(reviewLink('personal', `${key}x`)).toBeNull()
  })
})
