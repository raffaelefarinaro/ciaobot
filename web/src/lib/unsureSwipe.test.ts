import { describe, expect, it } from 'vitest'
import {
  SWIPE_MIN_PX, swipeAxis, swipeOutcome, swipeProgress, swipeThreshold, swipeTransform,
} from './unsureSwipe'

describe('unsureSwipe', () => {
  it('needs a quarter-ish of the card, never less than the minimum', () => {
    expect(swipeThreshold(390)).toBe(109)
    expect(swipeThreshold(200)).toBe(SWIPE_MIN_PX)
  })

  it('reads a press as a drag only once it moved, and mostly sideways', () => {
    expect(swipeAxis(4, 3)).toBeNull()
    expect(swipeAxis(20, 4)).toBe(true)
    expect(swipeAxis(4, 20)).toBe(false)
    // A diagonal is a scroll: sideways must clearly win.
    expect(swipeAxis(12, 11)).toBe(false)
  })

  it('commits past the threshold, or on a fast fling, and springs back otherwise', () => {
    expect(swipeOutcome(120, 600, 390)).toBe('approve')
    expect(swipeOutcome(-120, 600, 390)).toBe('dismiss')
    expect(swipeOutcome(100, 600, 390)).toBeNull()
    // Short but fast: a fling.
    expect(swipeOutcome(60, 80, 390)).toBe('approve')
    // Fast but too short to mean it.
    expect(swipeOutcome(30, 20, 390)).toBeNull()
    expect(swipeOutcome(0, 0, 390)).toBeNull()
  })

  it('stamps in proportion and tilts only when motion is allowed', () => {
    expect(swipeProgress(54.5, 390)).toBeCloseTo(0.5)
    expect(swipeProgress(-500, 390)).toBe(-1)
    expect(swipeTransform(0, 390, false)).toBe('')
    expect(swipeTransform(60, 390, false)).toMatch(/^translateX\(60px\) rotate\(2\.\d\ddeg\)$/)
    expect(swipeTransform(60, 390, true)).toBe('translateX(60px)')
  })
})
