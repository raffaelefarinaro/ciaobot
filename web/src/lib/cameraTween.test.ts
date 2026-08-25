import { describe, expect, it } from 'vitest'

import { easeOutCubic, tweenCamera, type CameraState } from './cameraTween'

const A: CameraState = { x: 0, y: 0, scale: 0.5 }
const B: CameraState = { x: -200, y: 100, scale: 2 }

describe('easeOutCubic', () => {
  it('pins the endpoints and clamps out-of-range input', () => {
    expect(easeOutCubic(0)).toBe(0)
    expect(easeOutCubic(1)).toBe(1)
    expect(easeOutCubic(-1)).toBe(0)
    expect(easeOutCubic(4)).toBe(1)
  })

  it('decelerates — more than half the distance is covered by the halfway point', () => {
    expect(easeOutCubic(0.5)).toBeGreaterThan(0.5)
    expect(easeOutCubic(0.25)).toBeGreaterThan(easeOutCubic(0.2))
  })
})

describe('tweenCamera', () => {
  it('returns the endpoints exactly', () => {
    expect(tweenCamera(A, B, 0)).toEqual(A)
    expect(tweenCamera(A, B, 1)).toEqual(B)
  })

  it('interpolates the translation linearly', () => {
    expect(tweenCamera(A, B, 0.5).x).toBeCloseTo(-100)
    expect(tweenCamera(A, B, 0.5).y).toBeCloseTo(50)
  })

  it('interpolates scale geometrically, so the midpoint is the geometric mean', () => {
    // A linear ramp would put the midpoint at 1.25 — visibly past halfway in
    // perceived zoom, which is what made the jump read as a lurch.
    expect(tweenCamera(A, B, 0.5).scale).toBeCloseTo(Math.sqrt(0.5 * 2))
  })

  it('clamps t rather than extrapolating past the target', () => {
    expect(tweenCamera(A, B, 2)).toEqual(B)
    expect(tweenCamera(A, B, -3)).toEqual(A)
  })

  it('survives a zero or negative scale instead of returning NaN', () => {
    const out = tweenCamera({ x: 0, y: 0, scale: 0 }, { x: 0, y: 0, scale: 0 }, 0.5)
    expect(Number.isFinite(out.scale)).toBe(true)
  })
})
