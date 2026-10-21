import { describe, expect, test } from 'vitest'
import { MEMORY_REASON_CHIPS } from './memoryDecisionReasons'

describe('MEMORY_REASON_CHIPS', () => {
  test('lists the quick reasons for a memory decision', () => {
    expect([...MEMORY_REASON_CHIPS]).toEqual([
      'already known', 'tool quirk — belongs in a skill', 'one-off', 'wrong place', 'not true', 'yes everywhere',
    ])
  })

  test('no label contains a comma, because the reason composes chips comma-separated', () => {
    for (const chip of MEMORY_REASON_CHIPS) expect(chip).not.toContain(',')
  })
})
