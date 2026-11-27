import { describe, expect, it } from 'vitest'
import {
  automationOrder, cadenceBadge, groupAutomations, lastRunLabel, lastRunState, lastRunWord,
  nextUpSchedules, relativeOffset, relativeWhen, scheduleTitle,
} from './scheduleList'
import type { Schedule } from './types'

function s(id: string, overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: id, title: id, prompt: 'p', daily_time_utc: '09:00', timezone_name: 'UTC',
    frequency: 'daily', enabled: true, scope: 'user', interval_minutes: 0, run_at_date: null,
    next_run: null, missed: false,
    ...overrides,
  } as Schedule
}

const loop = (id: string, overrides: Partial<Schedule> = {}) =>
  s(id, { frequency: 'interval', interval_minutes: 5, owner_chat_id: 'chat-a', web_chat_id: 'chat-a', ...overrides })

const ids = (list: Schedule[]) => list.map(x => x.schedule_id)

describe('groupAutomations', () => {
  it('puts system routines, the user\'s routines and loops each in their own list', () => {
    const g = groupAutomations([
      s('sys', { scope: 'system' }),
      loop('lp'),
      s('routine'),
      s('bound', { frequency: 'interval', interval_minutes: 30, web_chat_id: 'chat-b' }),
      s('once', { frequency: 'once', run_at_date: '2026-12-01' }),
      s('sys-interval', { scope: 'system', frequency: 'interval', interval_minutes: 60 }),
    ])
    expect(ids(g.system)).toEqual(['sys', 'sys-interval'])
    // One-offs are routines too; a chat-bound interval is not a loop unless the chat owns it.
    expect(ids(g.routines)).toEqual(['bound', 'once', 'routine'])
    expect(ids(g.loops)).toEqual(['lp'])
  })

  it('sorts by next run, undated after dated, and paused last in the same list', () => {
    const g = groupAutomations([
      s('paused-soon', { enabled: false, next_run: '2026-01-01T07:00:00Z' }),
      s('later', { next_run: '2026-01-01T10:00:00Z' }),
      s('manual', { frequency: 'manual' }),
      s('soon', { next_run: '2026-01-01T08:00:00Z' }),
      s('once', { frequency: 'once', next_run: '2026-01-01T09:00:00Z' }),
    ])
    expect(ids(g.routines)).toEqual(['soon', 'once', 'later', 'manual', 'paused-soon'])
  })

  it('walks the pages top to bottom: system, then the user\'s routines, then loops', () => {
    const list = [loop('lp'), s('routine'), s('sys', { scope: 'system' })]
    expect(ids(automationOrder(list))).toEqual(['sys', 'routine', 'lp'])
  })
})

describe('nextUpSchedules', () => {
  it('lists the soonest coming runs, without paused, undated or loop entries, at most six', () => {
    const at = (h: number) => `2026-01-01T${String(h).padStart(2, '0')}:00:00Z`
    const list = [
      s('r9', { next_run: at(9) }),
      s('sys', { scope: 'system', next_run: at(8) }),
      s('once', { frequency: 'once', next_run: at(7) }),
      s('paused', { enabled: false, next_run: at(1) }),
      s('manual', { frequency: 'manual' }),
      loop('lp', { next_run: at(2) }),
      s('missed', { missed: true, next_run: at(10) }),
      s('r11', { next_run: at(11) }),
      s('r12', { next_run: at(12) }),
      s('r13', { next_run: at(13) }),
    ]
    expect(ids(nextUpSchedules(list))).toEqual(['once', 'sys', 'r9', 'missed', 'r11', 'r12'])
    expect(ids(nextUpSchedules(list, 2))).toEqual(['once', 'sys'])
  })
})

describe('relative times', () => {
  const now = Date.parse('2026-01-02T12:00:00Z')
  it('says the distance in one unit', () => {
    expect(relativeOffset('2026-01-02T21:00:00Z', now)).toBe('in 9h')
    expect(relativeOffset('2026-01-02T05:00:00Z', now)).toBe('7h ago')
    expect(relativeOffset('2026-01-02T12:02:00Z', now)).toBe('in 2m')
    expect(relativeOffset('2026-01-05T12:00:00Z', now)).toBe('in 3d')
    expect(relativeOffset('2026-01-02T12:00:20Z', now)).toBe('now')
    expect(relativeOffset('', now)).toBe('')
    expect(relativeOffset('garbage', now)).toBe('')
  })
  it('puts the local clock before it', () => {
    expect(relativeWhen('2026-01-02T05:00:00Z', now)).toMatch(/ · 7h ago$/)
    expect(relativeWhen(null, now)).toBe('—')
  })
})

describe('lastRunState', () => {
  const idle = () => false
  it('ranks a missed slot over a failure, a failure over a run in progress, and that over a clean run', () => {
    expect(lastRunState(s('a', { missed: true, last_status: 'error' }), idle)).toBe('missed')
    expect(lastRunState(s('a', { last_status: 'error' }), idle)).toBe('failed')
    expect(lastRunState(s('a', { last_status: 'skipped' }), idle)).toBe('failed')
    expect(lastRunState(s('a', { last_status: 'missing-chat' }), idle)).toBe('failed')
    expect(lastRunState(s('a', { last_status: 'running' }), idle)).toBe('working')
    expect(lastRunState(s('a', { last_status: 'ok', last_run_chat_id: 'chat-1' }), id => id === 'chat-1')).toBe('working')
    expect(lastRunState(s('a', { last_status: 'ok', web_chat_id: 'chat-2' }), id => id === 'chat-2')).toBe('working')
    expect(lastRunState(s('a', { last_status: 'ok' }), idle)).toBe('done')
    expect(lastRunState(s('a'), idle)).toBe('none')
  })
  it('names the state in a word where it wants the owner, and in full for assistive tech', () => {
    expect(lastRunWord(s('a'), 'missed')).toBe('missed')
    expect(lastRunWord(s('a', { last_status: 'error' }), 'failed')).toBe('failed')
    expect(lastRunWord(s('a', { last_status: 'skipped' }), 'failed')).toBe('needs you')
    expect(lastRunWord(s('a'), 'done')).toBe('')
    expect(lastRunLabel(s('a'), 'missed')).toBe('Missed a run')
    expect(lastRunLabel(s('a'), 'working')).toBe('Running now')
  })
})

describe('scheduleTitle and cadenceBadge', () => {
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
