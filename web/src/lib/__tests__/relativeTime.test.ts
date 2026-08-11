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
})
