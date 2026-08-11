import { describe, expect, it } from 'vitest'
import { formatRelative } from '../relativeTime'

const now = new Date('2026-08-11T12:00:00Z')

function ago(seconds: number): string {
  return new Date(now.getTime() - seconds * 1000).toISOString()
}

describe('formatRelative', () => {
  it.each([
    [59, 'now'],
    [60, '1m'],
    [59 * 60, '59m'],
    [60 * 60, '1h'],
    [23 * 60 * 60, '23h'],
    [24 * 60 * 60, '1d'],
    [6 * 24 * 60 * 60, '6d'],
    [7 * 24 * 60 * 60, '1w'],
    [28 * 24 * 60 * 60, '1mo'],
    [120 * 24 * 60 * 60, '4mo'],
  ])('formats %s seconds as %s', (seconds, expected) => {
    expect(formatRelative(ago(seconds), now)).toBe(expected)
  })

  it('returns "now" for an unparseable timestamp', () => {
    expect(formatRelative('not-a-date', now)).toBe('now')
  })

  // The suffix form exists so ProjectView's file listings keep reading as prose
  // without the app growing a second relative-time implementation.
  describe('suffix form', () => {
    it.each([
      [30, 'just now'],
      [5 * 60, '5m ago'],
      [3 * 60 * 60, '3h ago'],
      [3 * 24 * 60 * 60, '3d ago'],
    ])('formats %s seconds as %s', (seconds, expected) => {
      expect(formatRelative(ago(seconds), { suffix: true, now })).toBe(expected)
    })
  })

  describe('absoluteAfterDays', () => {
    it('stays relative below the threshold', () => {
      expect(formatRelative(ago(3 * 24 * 60 * 60), { absoluteAfterDays: 7, now })).toBe('3d')
    })

    it('switches to an absolute date at the threshold', () => {
      // Beats "1w" when the exact day is what the reader wants.
      expect(formatRelative(ago(9 * 24 * 60 * 60), { absoluteAfterDays: 7, now }))
        .toBe(new Date(ago(9 * 24 * 60 * 60)).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))
    })

    it('preempts the month bucket too', () => {
      const result = formatRelative(ago(120 * 24 * 60 * 60), { absoluteAfterDays: 7, now })
      expect(result).not.toBe('4mo')
    })
  })
})
