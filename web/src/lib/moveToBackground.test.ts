import { describe, expect, it } from 'vitest'
import { canOfferMoveToBackground, nextBackgroundableCalls } from './moveToBackground'

describe('move to background helpers', () => {
  it('adds a backgroundable call once and keeps its first-seen time', () => {
    const first = nextBackgroundableCalls(undefined, { type: 'tool_use', tool_use_id: 'c1', backgroundable: true }, 10)
    expect(nextBackgroundableCalls(first, { type: 'tool_use', tool_use_id: 'c1', backgroundable: true }, 20)).toEqual({ c1: 10 })
  })

  it('removes a call when its tool_result frame arrives', () => {
    expect(nextBackgroundableCalls({ c1: 10 }, { type: 'tool_result', tool_use_id: 'c1' }, 20)).toEqual({})
  })

  it('ignores unmarked, id-less and subagent-internal frames', () => {
    const calls = { c1: 10 }
    expect(nextBackgroundableCalls(calls, { type: 'tool_use', tool_use_id: 'c2' }, 20)).toBe(calls)
    expect(nextBackgroundableCalls(calls, { type: 'tool_use', backgroundable: true }, 20)).toBe(calls)
    expect(nextBackgroundableCalls(calls, { type: 'tool_use', tool_use_id: 'c2', backgroundable: true, parent_tool_use_id: 'p1' }, 20)).toBe(calls)
  })

  it('offers the action only after the delay', () => {
    expect(canOfferMoveToBackground({ a: 0 }, 2999)).toBe(false)
    expect(canOfferMoveToBackground({ a: 0 }, 3000)).toBe(true)
    expect(canOfferMoveToBackground({}, 1e9)).toBe(false)
  })
})
