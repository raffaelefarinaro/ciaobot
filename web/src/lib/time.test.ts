import { describe, expect, it } from 'vitest'
import { formatDuration, formatRelative, minutesSince } from './time'

describe('formatRelative', () => {
  const now = new Date('2026-08-04T12:00:00Z')

  it('says how long ago, in the largest sensible unit', () => {
    expect(formatRelative('2026-08-04T11:59:50Z', now)).toBe('just now')
    expect(formatRelative('2026-08-04T11:57:00Z', now)).toBe('3 minutes ago')
    expect(formatRelative('2026-08-04T11:00:00Z', now)).toBe('1 hour ago')
    expect(formatRelative('2026-08-02T12:00:00Z', now)).toBe('2 days ago')
    expect(formatRelative('2026-06-04T12:00:00Z', now)).toBe('2 months ago')
    expect(formatRelative('2024-08-04T12:00:00Z', now)).toBe('2 years ago')
  })

  it('is quiet about missing or unparseable timestamps', () => {
    expect(formatRelative(undefined, now)).toBe('')
    expect(formatRelative('not a date', now)).toBe('')
  })

  it('does not report a clock-skewed future run as ages ago', () => {
    expect(formatRelative('2026-08-04T12:05:00Z', now)).toBe('just now')
  })
})

describe('formatDuration', () => {
  it('does not present a measured sub-millisecond duration as zero', () => {
    expect(formatDuration(0)).toBe('<1ms')
    expect(formatDuration(0.6)).toBe('<1ms')
    expect(formatDuration(1)).toBe('1ms')
  })
})

describe('minutesSince', () => {
  const start = Date.parse('2026-10-06T10:00:00Z')

  it('rounds down to whole minutes, so a ticking clock does not jitter the width', () => {
    expect(minutesSince('2026-10-06T10:00:00Z', start + 4_300)).toBe('<1m')
    expect(minutesSince('2026-10-06T10:00:00Z', start + 125_400)).toBe('2m')
    expect(minutesSince('2026-10-06T10:00:00Z', start + 3_725_000)).toBe('1h 2m')
  })

  it('is quiet about a missing or unparseable start', () => {
    expect(minutesSince('', start)).toBe('')
    expect(minutesSince('nope', start)).toBe('')
  })
})
