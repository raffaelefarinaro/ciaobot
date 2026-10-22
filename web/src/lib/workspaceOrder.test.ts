import { describe, expect, it } from 'vitest'
import { moveAnnouncement, moveName } from './workspaceOrder'

describe('moveName', () => {
  const names = ['a', 'b', 'c', 'd']

  it('moves a name down', () => {
    expect(moveName(names, 0, 1)).toEqual(['b', 'a', 'c', 'd'])
  })

  it('moves a name up', () => {
    expect(moveName(names, 2, 1)).toEqual(['a', 'c', 'b', 'd'])
  })

  it('moves to the ends', () => {
    expect(moveName(names, 0, 3)).toEqual(['b', 'c', 'd', 'a'])
    expect(moveName(names, 3, 0)).toEqual(['d', 'a', 'b', 'c'])
  })

  it('returns the same contents for equal or out-of-range indices', () => {
    expect(moveName(names, 1, 1)).toEqual(names)
    expect(moveName(names, -1, 2)).toEqual(names)
    expect(moveName(names, 1, 4)).toEqual(names)
    expect(moveName(names, 9, 0)).toEqual(names)
  })

  it('never mutates the input and always returns a new array', () => {
    const input = ['a', 'b', 'c']
    const moved = moveName(input, 0, 2)
    expect(input).toEqual(['a', 'b', 'c'])
    expect(moved).not.toBe(input)
    expect(moveName(input, 1, 1)).not.toBe(input)
  })
})

describe('moveAnnouncement', () => {
  it('names the label and the one-based position', () => {
    expect(moveAnnouncement('Work', 2, 3)).toBe('Moved Work to position 2 of 3')
  })
})
