import { describe, expect, it } from 'vitest'
import { cadenceBadge, groupSidebarSchedules, scheduleTitle, sidebarScheduleOrder } from './scheduleList'
import type { Schedule } from './types'

function s(id: string, overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: id, title: id, prompt: 'p', daily_time_utc: '09:00', timezone_name: 'UTC',
    frequency: 'daily', enabled: true, scope: 'user', interval_minutes: 0, run_at_date: null,
    ...overrides,
  } as Schedule
}

describe('scheduleList', () => {
  it('groups one-offs (soonest first), routines and system, and flattens them in that order', () => {
    const list = [
      s('sys', { scope: 'system' }),
      s('late', { frequency: 'once', run_at_date: '2026-12-02' }),
      s('routine'),
      s('soon', { frequency: 'once', run_at_date: '2026-12-01' }),
    ]
    const g = groupSidebarSchedules(list)
    expect(g.oneOffs.map(x => x.schedule_id)).toEqual(['soon', 'late'])
    expect(g.routines.map(x => x.schedule_id)).toEqual(['routine'])
    expect(g.system.map(x => x.schedule_id)).toEqual(['sys'])
    expect(sidebarScheduleOrder(list).map(x => x.schedule_id)).toEqual(['soon', 'late', 'routine', 'sys'])
  })

  it('titles an untitled schedule from its prompt\'s first line, truncated', () => {
    expect(scheduleTitle({ title: '', prompt: 'Short line\nmore' })).toBe('Short line')
    expect(scheduleTitle({ title: '', prompt: 'x'.repeat(40) })).toBe('x'.repeat(33) + '...')
    expect(scheduleTitle({ title: 'Named', prompt: 'ignored' })).toBe('Named')
  })

  it('badges paused, interval and manual schedules', () => {
    expect(cadenceBadge(s('a', { enabled: false }))).toBe('off')
    expect(cadenceBadge(s('a', { frequency: 'interval', interval_minutes: 15 }))).toBe('15m')
    expect(cadenceBadge(s('a', { frequency: 'manual' }))).toBe('·')
  })
})
