import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api', () => ({ api: { get: vi.fn() } }))

import { api } from './api'
import {
  STATS_RANGES,
  fetchUsageStats,
  formatCompact,
  formatCount,
  formatDelta,
  normalizeUsageStats,
  periodLabel,
  rangeLabel,
} from './usageStats'

const get = vi.mocked(api.get)

beforeEach(() => {
  get.mockReset()
})

describe('normalizeUsageStats', () => {
  it('normalizes an empty or wrong-shaped body to all-null stats', () => {
    for (const raw of [{}, []]) {
      const s = normalizeUsageStats(raw, 30)
      expect(s.range_days).toBe(30)
      expect(Object.values(s.totals).every(v => v === null)).toBe(true)
      expect(Object.values(s.previous).every(v => v === null)).toBe(true)
      expect(Object.values(s.today).every(v => v === null)).toBe(true)
      expect(Object.values(s.memory_pipeline).every(v => v === null)).toBe(true)
      expect(s.hours).toHaveLength(24)
      expect(s.hours.every(v => v === null)).toBe(true)
      expect(s.daily_messages).toEqual([])
      expect(s.projects).toEqual([])
      expect(s.models).toEqual([])
      expect(s.generated_at).toBe('')
    }
  })

  it('keeps finite numbers and nulls everything else', () => {
    const s = normalizeUsageStats({
      totals: { chats_opened: 5, tokens: '5', messages_sent: Infinity, chats_archived: NaN },
    }, 30)
    expect(s.totals.chats_opened).toBe(5)
    expect(s.totals.tokens).toBeNull()
    expect(s.totals.messages_sent).toBeNull()
    expect(s.totals.chats_archived).toBeNull()
  })

  it('pads hours to 24 and trims longer arrays', () => {
    const short = normalizeUsageStats({ hours: [1, 2] }, 30).hours
    expect(short).toHaveLength(24)
    expect(short[0]).toBe(1)
    expect(short[23]).toBeNull()

    const long = normalizeUsageStats({ hours: Array.from({ length: 30 }, () => 1) }, 30).hours
    expect(long).toHaveLength(24)
  })

  it('drops days without an ISO date and projects/models without a name', () => {
    const s = normalizeUsageStats({
      daily_messages: [{ date: '2026-11-09', count: 3 }, { date: 'today', count: 1 }, { count: 2 }],
      projects: [{ project_id: 'p', messages: 2 }, {}],
      models: [{ turns: 3 }, { model: 'm', turns: 'x' }],
    }, 30)
    expect(s.daily_messages).toEqual([{ date: '2026-11-09', count: 3 }])
    expect(s.projects).toEqual([{ project_id: 'p', name: 'p', messages: 2 }])
    expect(s.models).toEqual([{ model: 'm', turns: null }])
  })
})

describe('fetchUsageStats', () => {
  it('asks for the workspace and range, then normalizes', async () => {
    get.mockResolvedValue({ range_days: 90 })
    const s = await fetchUsageStats('personal', 90)
    expect(get).toHaveBeenCalledWith('/api/stats?workspace=personal&range=90')
    expect(s.range_days).toBe(90)
    expect(s.hours).toHaveLength(24)
  })
})

describe('formatters', () => {
  it('formatCount', () => {
    expect(formatCount(null)).toBe('—')
    expect(formatCount(0)).toBe('0')
    expect(formatCount(4517)).toBe('4,517')
    expect(formatCount(10.4)).toBe('10')
  })

  it('formatCompact', () => {
    expect(formatCompact(null)).toBe('—')
    expect(formatCompact(999)).toBe('999')
    expect(formatCompact(1000)).toBe('1K')
    expect(formatCompact(71500000)).toBe('71.5M')
    expect(formatCompact(204000000)).toBe('204M')
    expect(formatCompact(1200000000)).toBe('1.2B')
  })

  it('formatDelta', () => {
    expect(formatDelta(248, 210)).toBe('+18%')
    expect(formatDelta(61, 63)).toBe('-3%')
    expect(formatDelta(100, 100)).toBe('0%')
    expect(formatDelta(100, 100.2)).toBe('0%')
    expect(formatDelta(5, 0)).toBeNull()
    expect(formatDelta(null, 3)).toBeNull()
    expect(formatDelta(3, null)).toBeNull()
  })

  it('labels ranges and periods', () => {
    expect(rangeLabel(7)).toBe('7 days')
    expect(rangeLabel(365)).toBe('Year')
    expect(periodLabel(30)).toBe('previous 30 days')
    expect(periodLabel(365)).toBe('previous year')
    expect(STATS_RANGES).toEqual([7, 30, 90, 365])
  })
})
