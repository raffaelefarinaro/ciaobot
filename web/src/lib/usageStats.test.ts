import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api', () => ({ api: { get: vi.fn() } }))

import { api } from './api'
import { modelDisplayName } from './modelLabels'
import {
  STATS_RANGES,
  barPercent,
  busiestHour,
  fetchUsageStats,
  formatCompact,
  formatAverage,
  formatCount,
  formatDelta,
  formatDay,
  heatCellLabel,
  heatLevel,
  heatmapCells,
  heatmapMonths,
  modelShares,
  normalizeUsageStats,
  periodLabel,
  periodText,
  rangeLabel,
  topProjects,
  weekdayIndex,
} from './usageStats'
import type { UsageDay, UsageModel, UsageProject } from './usageStats'

function isoDay(end: string, daysBack: number): string {
  const [y, m, d] = end.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d - daysBack)).toISOString().slice(0, 10)
}

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
  it('formatAverage keeps a decimal below 10', () => {
    expect(formatAverage(null)).toBe('—')
    expect(formatAverage(0.4)).toBe('0.4')
    expect(formatAverage(2)).toBe('2')
    expect(formatAverage(9.94)).toBe('9.9')
    expect(formatAverage(10.4)).toBe('10')
    expect(formatAverage(1234.6)).toBe('1,235')
  })

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

describe('breakdown helpers', () => {
  it('levels a day against the busiest day', () => {
    expect(heatLevel(0, 10)).toBe(0)
    expect(heatLevel(null, 10)).toBe(0)
    expect(heatLevel(5, 0)).toBe(0)
    expect(heatLevel(1, 10)).toBe(1)
    expect(heatLevel(3, 10)).toBe(2)
    expect(heatLevel(6, 10)).toBe(3)
    expect(heatLevel(10, 10)).toBe(4)
  })

  it('names weekdays and days', () => {
    expect(weekdayIndex('2026-11-09')).toBe(0)
    expect(weekdayIndex('2026-11-04')).toBe(2)
    expect(weekdayIndex('2026-11-15')).toBe(6)
    expect(formatDay('2026-11-09')).toBe('Mon 9 Nov 2026')
  })

  it('pads the heatmap to the first day weekday', () => {
    const cells = heatmapCells([
      { date: '2026-11-04', count: 3 },
      { date: '2026-11-05', count: 0 },
      { date: '2026-11-06', count: null },
    ])
    expect(cells).toHaveLength(5)
    expect(cells[0].date).toBeNull()
    expect(cells[1].date).toBeNull()
    expect(cells[2]).toEqual({ date: '2026-11-04', count: 3, level: 4 })
    expect(cells[3].level).toBe(0)
    expect(cells[4]).toMatchObject({ count: null, level: 0 })
    expect(heatmapCells([])).toEqual([])
  })

  it('covers 364 days in 52 or 53 columns', () => {
    const days: UsageDay[] = Array.from({ length: 364 }, (_, i) => ({ date: isoDay('2026-11-09', 363 - i), count: 1 }))
    expect(days[0].date).toBe('2025-11-11')
    const cells = heatmapCells(days)
    expect(cells).toHaveLength(365)
    expect(cells.filter(c => c.date === null)).toHaveLength(1)
  })

  it('names each month at the week column where it starts', () => {
    const days: UsageDay[] = Array.from({ length: 364 }, (_, i) => ({ date: isoDay('2026-11-09', 363 - i), count: 1 }))
    const months = heatmapMonths(heatmapCells(days))
    // Nov 2025 starts the strip with only three weeks left, so it keeps its label.
    expect(months.map(m => m.label)).toEqual(['Nov', 'Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov'])
    expect(months[0]).toEqual({ column: 1, label: 'Nov' })
    expect(months[1]).toEqual({ column: 4, label: 'Dec' })
    expect(months.every((m, i) => i === 0 || m.column > months[i - 1]!.column)).toBe(true)
  })

  it('drops a partial first month that would collide with the next label', () => {
    const days: UsageDay[] = Array.from({ length: 20 }, (_, i) => ({ date: isoDay('2026-11-09', 19 - i), count: 1 }))
    expect(heatmapMonths(heatmapCells(days)).map(m => m.label)).toEqual(['Nov'])
  })

  it('labels heatmap cells', () => {
    const cells = heatmapCells([
      { date: '2026-11-04', count: 3 },
      { date: '2026-11-05', count: 1 },
      { date: '2026-11-06', count: null },
    ])
    expect(heatCellLabel(cells[2])).toBe('Wed 4 Nov 2026: 3 messages')
    expect(heatCellLabel(cells[3])).toBe('Thu 5 Nov 2026: 1 message')
    expect(heatCellLabel(cells[4])).toBe('Fri 6 Nov 2026: no data')
  })

  it('sizes bars against the largest value', () => {
    expect(barPercent(206, 412)).toBe('50%')
    expect(barPercent(412, 412)).toBe('100%')
    expect(barPercent(1, 3)).toBe('33.3%')
    expect(barPercent(null, 10)).toBe('0%')
    expect(barPercent(5, 0)).toBe('0%')
  })

  it('finds the busiest hour', () => {
    const hours: Array<number | null> = Array.from({ length: 24 }, () => 0)
    hours[21] = 17
    hours[9] = 14
    expect(busiestHour(hours)).toBe(21)
    expect(busiestHour(Array.from({ length: 24 }, () => null))).toBeNull()
    expect(busiestHour(Array.from({ length: 24 }, () => 0))).toBeNull()
  })

  it('ranks projects and caps them', () => {
    const list: UsageProject[] = [
      ...Array.from({ length: 10 }, (_, i) => ({ project_id: `p${i + 1}`, name: `P${i + 1}`, messages: i + 1 })),
      { project_id: 'none', name: 'None', messages: null },
    ]
    const top = topProjects(list)
    expect(top).toHaveLength(8)
    expect(top[0].messages).toBe(10)
    expect(topProjects(list, 3)).toHaveLength(3)
  })

  it('shares turns between models', () => {
    const models: UsageModel[] = [{ model: 'b', turns: 22 }, { model: 'a', turns: 68 }, { model: 'c', turns: 10 }]
    const shares = modelShares(models)
    expect(shares.map(s => s.model)).toEqual(['a', 'b', 'c'])
    expect(shares.map(s => s.percent)).toEqual([68, 22, 10])
    expect(shares[0].label).toBe(modelDisplayName('a'))
    expect(modelShares([{ model: 'x', turns: null }, { model: 'y', turns: null }]).map(s => s.percent)).toEqual([0, 0])
  })

  it('words the period', () => {
    expect(periodText(30)).toBe('30 days')
    expect(periodText(365)).toBe('year')
  })
})
