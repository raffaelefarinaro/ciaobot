import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Schedule } from './types'
import { cadenceSummary, localAt, oneOffSortKey, oneOffWhen, scheduleClock } from './scheduleClock'

// Every expectation here is about the browser's clock, so each test pins TZ.
// Assigning process.env.TZ at runtime makes Node re-read the zone for Date.
let previousTz: string | undefined
beforeEach(() => {
  previousTz = process.env.TZ
})
afterEach(() => {
  if (previousTz === undefined) delete process.env.TZ
  else process.env.TZ = previousTz
})
function pinTz(zone: string) {
  process.env.TZ = zone
}

function sched(overrides: Partial<Schedule>): Schedule {
  return {
    schedule_id: 's',
    daily_time_utc: '00:01',
    prompt: 'p',
    chat_id: 0,
    created_at: '2026-01-01T00:00:00Z',
    timezone_name: 'UTC',
    last_triggered_on: '',
    days_of_week: null,
    thread_id: null,
    context_label: 'General',
    frequency: 'daily',
    interval_minutes: 0,
    day_of_month: null,
    run_at_date: null,
    web_chat_id: null,
    web_project_id: 'proj-1',
    workspace: 'work',
    model: '',
    next_run: null,
    last_expected_run: null,
    missed: false,
    enabled: true,
    archive_policy: 'manual',
    ...overrides,
  } as Schedule
}

// A fixed "now" well away from any boundary so relative logic cannot interfere.
const NOW = Date.parse('2026-11-20T12:00:00Z')

describe('cadenceSummary in local time', () => {
  it('converts a UTC daily time to Europe/Rome (CET, +1)', () => {
    pinTz('Europe/Rome')
    const s = sched({ next_run: '2026-12-05T00:01:00Z' })
    expect(cadenceSummary(s, NOW)).toBe('Every day at 01:01')
  })

  it('keeps the same clock when the schedule zone matches the browser zone', () => {
    pinTz('Europe/Rome')
    const s = sched({ timezone_name: 'Europe/Rome', daily_time_utc: '08:00', next_run: '2026-12-05T07:00:00Z' })
    expect(cadenceSummary(s, NOW)).toBe('Every day at 08:00')
  })

  it('uses the offset in force at the next run, so DST is handled', () => {
    // New York 09:00 is 13:00 UTC in summer (EDT), which is 15:00 in Rome (CEST).
    pinTz('Europe/Rome')
    const s = sched({
      timezone_name: 'America/New_York',
      daily_time_utc: '09:00',
      next_run: '2026-07-01T13:00:00Z',
    })
    expect(cadenceSummary(s, NOW)).toBe('Every day at 15:00')
  })
})

describe('weekly cadence crossing midnight', () => {
  it('shifts the weekday forward when local is the next day (Tokyo)', () => {
    // Sat 20:00 UTC is Sun 05:00 in Tokyo.
    pinTz('Asia/Tokyo')
    const s = sched({
      frequency: 'weekly',
      days_of_week: ['sat'],
      daily_time_utc: '20:00',
      next_run: '2026-12-05T20:00:00Z',
    })
    expect(localAt(s, NOW)?.dayShift).toBe(1)
    expect(cadenceSummary(s, NOW)).toBe('Weekly on Sun at 05:00')
  })

  it('shifts the weekday back when local is the previous day (New York)', () => {
    // Sun 01:00 UTC is Sat 20:00 in New York (EST, -5).
    pinTz('America/New_York')
    const s = sched({
      frequency: 'weekly',
      days_of_week: ['sun'],
      daily_time_utc: '01:00',
      next_run: '2026-12-06T01:00:00Z',
    })
    expect(localAt(s, NOW)?.dayShift).toBe(-1)
    expect(cadenceSummary(s, NOW)).toBe('Weekly on Sat at 20:00')
  })

  it('wraps Sunday to Monday and keeps the list in week order', () => {
    pinTz('Asia/Tokyo')
    const s = sched({
      frequency: 'weekly',
      days_of_week: ['sat', 'sun'],
      daily_time_utc: '20:00',
      next_run: '2026-12-05T20:00:00Z',
    })
    expect(cadenceSummary(s, NOW)).toBe('Weekly on Mon, Sun at 05:00')
  })

  it('does not shift days when the conversion stays on the same day', () => {
    pinTz('Europe/Rome')
    const s = sched({
      frequency: 'weekly',
      days_of_week: ['sat'],
      daily_time_utc: '00:30',
      next_run: '2026-12-05T00:30:00Z',
    })
    expect(localAt(s, NOW)?.dayShift).toBe(0)
    expect(cadenceSummary(s, NOW)).toBe('Weekly on Sat at 01:30')
  })
})

describe('monthly cadence crossing midnight', () => {
  it('names the schedule zone instead of moving the day of month', () => {
    // Day 1 at 20:00 UTC is day 2 at 05:00 in Tokyo; moving "day 1" is ambiguous.
    pinTz('Asia/Tokyo')
    const s = sched({
      frequency: 'monthly',
      day_of_month: 1,
      daily_time_utc: '20:00',
      next_run: '2026-12-01T20:00:00Z',
    })
    expect(cadenceSummary(s, NOW)).toBe('Monthly on day 1 at 20:00 UTC')
    expect(scheduleClock(s, NOW)).toBe('20:00 UTC')
  })

  it('converts normally when the day does not change', () => {
    pinTz('Europe/Rome')
    const s = sched({
      frequency: 'monthly',
      day_of_month: 1,
      daily_time_utc: '00:30',
      next_run: '2026-12-01T00:30:00Z',
    })
    expect(cadenceSummary(s, NOW)).toBe('Monthly on day 1 at 01:30')
  })
})

describe('one-off entries', () => {
  it('converts both the date and the time to local', () => {
    // 21:00 on Dec 5 in New York (EST, -5) is 02:00 UTC on Dec 6, which is
    // 11:00 on Dec 6 in Tokyo: the date moves as well as the time.
    pinTz('Asia/Tokyo')
    const s = sched({
      frequency: 'once',
      run_at_date: '2026-12-05',
      daily_time_utc: '21:00',
      timezone_name: 'America/New_York',
      next_run: '2026-12-06T02:00:00Z',
    })
    expect(cadenceSummary(s, NOW)).toBe('Once on 2026-12-06 at 11:00')
    expect(oneOffWhen(s, NOW)).toBe('12-06 11:00')
  })

  it('sorts by full local date so later years do not sort first', () => {
    pinTz('UTC')
    const later = sched({ frequency: 'once', run_at_date: '2027-01-02', daily_time_utc: '09:00' })
    const sooner = sched({ frequency: 'once', run_at_date: '2026-12-31', daily_time_utc: '09:00' })
    const keys = [oneOffSortKey(later, NOW), oneOffSortKey(sooner, NOW)].sort()
    expect(keys[0]).toContain('2026-12-31')
  })
})

describe('sidebar clock', () => {
  it('shows the local time for a daily schedule', () => {
    pinTz('Europe/Rome')
    const s = sched({ next_run: '2026-12-05T00:01:00Z' })
    expect(scheduleClock(s, NOW)).toBe('01:01')
  })

  it('returns an empty clock when there is no time of day', () => {
    pinTz('Europe/Rome')
    expect(scheduleClock(sched({ daily_time_utc: '' }), NOW)).toBe('')
  })
})

describe('other cadences are unchanged', () => {
  it('describes interval and manual entries without a clock', () => {
    pinTz('Europe/Rome')
    expect(cadenceSummary(sched({ frequency: 'interval', interval_minutes: 30 }), NOW)).toBe('Every 30 min')
    expect(cadenceSummary(sched({ frequency: 'manual' }), NOW)).toBe('Only when you run it')
  })
})
