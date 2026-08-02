import { describe, expect, it } from 'vitest'
import { formatDuration } from './time'

describe('formatDuration', () => {
  it('does not present a measured sub-millisecond duration as zero', () => {
    expect(formatDuration(0)).toBe('<1ms')
    expect(formatDuration(0.6)).toBe('<1ms')
    expect(formatDuration(1)).toBe('1ms')
  })
})
