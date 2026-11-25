import { describe, expect, it } from 'vitest'
import { ringShape } from './ciaoRing'

describe('ringShape', () => {
  it('rests as the C with its two dots at their artwork positions', () => {
    const s = ringShape(0)
    expect(s.rotate).toBe(0)
    expect(s.arcA).toBe('')
    expect(s.arcB).toBe('')
    expect(s.dotA.cx).toBeCloseTo(870, 0)
    expect(s.dotA.cy).toBeCloseTo(346, 0)
    expect(s.dotA.r).toBe(91)
    expect(s.dotB.cx).toBeCloseTo(863, 0)
    expect(s.dotB.cy).toBeCloseTo(800, 0)
    expect(s.dotB.r).toBe(56)
  })

  it('closes into one full turn with both dots meeting on the ring', () => {
    const s = ringShape(1)
    expect(s.rotate).toBeCloseTo(360)
    expect(s.arcA).not.toBe('')
    expect(s.arcB).not.toBe('')
    expect(s.dotA.cx).toBeCloseTo(s.dotB.cx, 3)
    expect(s.dotA.cy).toBeCloseTo(s.dotB.cy, 3)
    expect(s.dotA.r).toBe(55)
  })

  it('lands the dots before it grows the arcs', () => {
    expect(ringShape(0.2).arcA).toBe('')
    expect(ringShape(0.2).dotA.r).toBeLessThan(91)
    expect(ringShape(0.6).arcA).not.toBe('')
  })

  it('clamps outside 0..1', () => {
    expect(ringShape(-3)).toEqual(ringShape(0))
    expect(ringShape(7)).toEqual(ringShape(1))
  })
})
