import { describe, expect, it } from 'vitest'
import { isOwnedLoop, loopEndsLabel, loopRunsLeft, loopRunsLeftLabel, loopShortLabel, loopSummary, loopsForChat } from './loops'
import type { Schedule } from './types'

function loop(id: string, overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: id, title: id, prompt: 'p', daily_time_utc: '', timezone_name: 'UTC',
    frequency: 'interval', enabled: true, scope: 'user', interval_minutes: 5, run_at_date: null,
    web_chat_id: 'chat-a', owner_chat_id: 'chat-a', expires_at: '2026-11-21T14:30:00Z',
    max_runs: 10, run_count: 3,
    ...overrides,
  } as Schedule
}

// 2026-11-21 12:00 UTC: "today" in UTC for the 14:30 end.
const NOW = Date.parse('2026-11-21T12:00:00Z')
const ZONE = 'UTC'

describe('loops', () => {
  it('treats only an owner-bound schedule as a loop', () => {
    expect(isOwnedLoop(loop('a'))).toBe(true)
    expect(isOwnedLoop(loop('a', { owner_chat_id: '' }))).toBe(false)
    expect(isOwnedLoop({} as Schedule)).toBe(false)
    expect(isOwnedLoop(loop('a', { owner_chat_id: undefined }))).toBe(false)
  })

  it('finds loops by owner chat, not by any chat-bound automation', () => {
    const list = [
      loop('own'),
      loop('other', { owner_chat_id: 'chat-b', web_chat_id: 'chat-b' }),
      // A routine bound to chat-a through web_chat_id is not a loop of chat-a.
      loop('routine', { owner_chat_id: undefined, web_chat_id: 'chat-a', frequency: 'daily' }),
    ]
    expect(loopsForChat(list, 'chat-a').map(s => s.schedule_id)).toEqual(['own'])
    expect(loopsForChat(list, '')).toEqual([])
  })

  it('counts runs left and floors at zero', () => {
    expect(loopRunsLeft(loop('a'))).toBe(7)
    expect(loopRunsLeft(loop('a', { run_count: 10 }))).toBe(0)
    expect(loopRunsLeft(loop('a', { run_count: 12 }))).toBe(0)
    expect(loopRunsLeft(loop('a', { max_runs: undefined, run_count: undefined }))).toBe(0)
  })

  it('shows the end time alone when it is today, with the date when it is not', () => {
    expect(loopEndsLabel(loop('a'), NOW, 'en-GB', ZONE)).toBe('ends 14:30')
    const tomorrow = Date.parse('2026-11-22T12:00:00Z')
    expect(loopEndsLabel(loop('a'), tomorrow, 'en-GB', ZONE)).toMatch(/^ends \S+ \d{1,2} \S+, 14:30$/)
  })

  it('never renders Invalid Date for a missing or malformed end', () => {
    expect(loopEndsLabel(loop('a', { expires_at: '' }), NOW)).toBe('ends soon')
    expect(loopEndsLabel(loop('a', { expires_at: 'not a date' }), NOW)).toBe('ends soon')
    expect(loopEndsLabel(loop('a', { expires_at: undefined }), NOW)).toBe('ends soon')
    expect(loopSummary(loop('a', { expires_at: 'x' }), NOW)).not.toContain('Invalid')
  })

  it('builds the full summary with cadence, end and runs left', () => {
    expect(loopSummary(loop('a'), NOW, 'en-GB', ZONE)).toBe('every 5 min · ends 14:30 · 7 runs left')
    expect(loopSummary(loop('a', { run_count: 9 }), NOW, 'en-GB', ZONE)).toBe('every 5 min · ends 14:30 · 1 run left')
    expect(loopSummary(loop('a', { run_count: 10 }), NOW, 'en-GB', ZONE)).toBe('every 5 min · ends 14:30 · 0 runs left')
  })

  it('tolerates an older engine payload that has no loop fields', () => {
    const old = loop('a', { owner_chat_id: undefined, expires_at: undefined, max_runs: undefined, run_count: undefined })
    expect(isOwnedLoop(old)).toBe(false)
    expect(loopsForChat([old], 'chat-a')).toEqual([])
  })

  it('words the runs left in the singular for one', () => {
    expect(loopRunsLeftLabel(loop('a', { run_count: 9 }))).toBe('1 run left')
    expect(loopRunsLeftLabel(loop('a', { run_count: 10 }))).toBe('0 runs left')
  })

  it('gives a short label for narrow composers', () => {
    expect(loopShortLabel(loop('a'))).toBe('Loop · 5m')
  })
})
