import { describe, expect, test } from 'vitest'
import { REASON_CHIPS, REASON_MAX, chipsIn, cleanReason, toggleChip } from './decisionReasons'

describe('decisionReasons', () => {
  test('toggleChip adds a chip to empty text as the chip alone', () => {
    expect(toggleChip('', 'not mine')).toBe('not mine')
  })

  test('toggleChip appends with a comma separator', () => {
    expect(toggleChip('because', 'not mine')).toBe('because, not mine')
  })

  test('toggleChip removes a chip that is present, ignoring case', () => {
    expect(toggleChip('not mine, Wrong Person', 'wrong person')).toBe('not mine')
    expect(toggleChip('not mine', 'not mine')).toBe('')
  })

  test('toggleChip never grows past the limit', () => {
    const long = 'x'.repeat(REASON_MAX - 3)
    expect(toggleChip(long, 'not mine')).toBe(long)
  })

  test('chipsIn lists the quick reasons present in the text', () => {
    expect(chipsIn('Not Mine, something else,good as is')).toEqual(['not mine', 'good as is'])
    expect(chipsIn('')).toEqual([])
  })

  test('cleanReason trims and caps', () => {
    expect(cleanReason('  hi  ')).toBe('hi')
    expect(cleanReason('y'.repeat(REASON_MAX + 5))).toHaveLength(REASON_MAX)
  })

  test('the chips are the five documented ones', () => {
    expect([...REASON_CHIPS]).toEqual(['not mine', 'not a task', 'already done', 'wrong person', 'good as is'])
  })
})
