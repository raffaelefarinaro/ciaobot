import { describe, expect, it } from 'vitest'
import { railItemLabel, railNeighbours, settledIndex, stableRailOrder } from './chatRail'

describe('stableRailOrder', () => {
  it('returns fresh when there is no snapshot', () => {
    expect(stableRailOrder(['a', 'b'], null)).toEqual(['a', 'b'])
  })

  it('keeps the snapshot order and drops vanished ids', () => {
    expect(stableRailOrder(['c', 'a'], ['a', 'b', 'c'])).toEqual(['a', 'c'])
  })

  it('appends new ids in fresh order', () => {
    expect(stableRailOrder(['n2', 'a', 'n1'], ['a'])).toEqual(['a', 'n2', 'n1'])
  })
})

describe('railNeighbours', () => {
  it('has no wrap-around', () => {
    expect(railNeighbours(['a', 'b', 'c'], 'a')).toEqual({ index: 0, previous: null, next: 'b' })
    expect(railNeighbours(['a', 'b', 'c'], 'c')).toEqual({ index: 2, previous: 'b', next: null })
  })

  it('reports an absent chat', () => {
    expect(railNeighbours(['a'], 'z')).toEqual({ index: -1, previous: null, next: null })
  })
})

describe('settledIndex', () => {
  it('picks the nearest centre', () => {
    expect(settledIndex(260, [100, 250, 400])).toBe(1)
  })

  it('breaks ties toward the lower index', () => {
    expect(settledIndex(175, [100, 250])).toBe(0)
  })

  it('returns -1 for an empty list', () => {
    expect(settledIndex(5, [])).toBe(-1)
  })
})

describe('railItemLabel', () => {
  it('names title, project and state', () => {
    expect(railItemLabel({ title: 'Fix CI', project: 'General', tier: 'needsYou', titlePending: false }))
      .toBe('Fix CI, General, needs you')
  })

  it('omits the state word for a quiet chat', () => {
    expect(railItemLabel({ title: 'Fix CI', project: 'General', tier: 'quiet', titlePending: false }))
      .toBe('Fix CI, General')
  })

  it('uses the naming placeholder for a pending title', () => {
    expect(railItemLabel({ title: 'New chat', project: 'General', tier: 'quiet', titlePending: true }))
      .toBe('Naming conversation, General')
  })

  it('omits the project when it is empty', () => {
    expect(railItemLabel({ title: 'Fix CI', project: '', tier: 'quiet', titlePending: false }))
      .toBe('Fix CI')
  })
})
