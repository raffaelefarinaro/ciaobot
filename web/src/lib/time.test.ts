import { describe, expect, it } from 'vitest'
import { formatDuration, formatRelative } from './time'

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
