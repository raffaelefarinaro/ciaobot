import { describe, expect, it } from 'vitest'
import {
  autoScrollStep,
  exceedsThreshold,
  insertionIndex,
  moveAnnouncement,
  previewEntries,
  settledOrder,
} from './dragSort'

const slots = (heights: number[], gap = 0) => {
  let top = 0
  return heights.map((height, i) => {
    const slot = { id: `s${i}`, top, height }
    top += height + gap
    return slot
  })
}

describe('dragSort', () => {
  it('insertionIndex handles first, middle, last and empty', () => {
    const s = slots([40, 40, 40])
    expect(insertionIndex(s, -10)).toBe(0)
    expect(insertionIndex(s, 10)).toBe(0)
    expect(insertionIndex(s, 50)).toBe(1)
    expect(insertionIndex(s, 90)).toBe(2)
    expect(insertionIndex(s, 500)).toBe(3)
    expect(insertionIndex([], 100)).toBe(0)
  })

  it('insertionIndex is stable for variable heights and tall cards', () => {
    const s = slots([20, 200, 20], 8)
    // Midpoints: 10, 128, 246.
    expect(insertionIndex(s, 127)).toBe(1)
    expect(insertionIndex(s, 129)).toBe(2)
    expect(insertionIndex(s, 245)).toBe(2)
    expect(insertionIndex(s, 247)).toBe(3)
  })

  it('previewEntries places the placeholder and keeps the dragged item', () => {
    const ids = ['a', 'b', 'c']
    const ph = { kind: 'placeholder' }
    const item = (id: string) => ({ kind: 'item', id })
    // Same list: b dragged, placeholder before a.
    expect(previewEntries(ids, 'b', 0)).toEqual([ph, item('a'), item('b'), item('c')])
    // Index 1 of the non-dragged [a, c]: after a.
    expect(previewEntries(ids, 'b', 1)).toEqual([item('a'), ph, item('b'), item('c')])
    expect(previewEntries(ids, 'b', 2)).toEqual([item('a'), item('b'), item('c'), ph])
    // Dragged item from another scope is simply absent.
    expect(previewEntries(['a', 'c'], 'x', 1)).toEqual([item('a'), ph, item('c')])
    expect(previewEntries([], 'x', 0)).toEqual([ph])
    // Index clamps.
    expect(previewEntries(ids, 'b', 99)).toEqual([item('a'), item('b'), item('c'), ph])
    expect(previewEntries(ids, 'b', -3)).toEqual([ph, item('a'), item('b'), item('c')])
  })

  it('settledOrder moves the dragged item to its target and clamps the index', () => {
    expect(settledOrder(['a', 'b', 'c'], 'a', 2)).toEqual(['b', 'c', 'a'])
    expect(settledOrder(['a', 'b', 'c'], 'c', 0)).toEqual(['c', 'a', 'b'])
    expect(settledOrder(['a', 'b', 'c'], 'b', 1)).toEqual(['a', 'b', 'c'])
    expect(settledOrder(['b', 'c'], 'a', 1)).toEqual(['b', 'a', 'c'])
    expect(settledOrder(['a', 'b', 'c'], 'a', 99)).toEqual(['b', 'c', 'a'])
    expect(settledOrder(['a', 'b', 'c'], 'a', -3)).toEqual(['a', 'b', 'c'])
  })

  it('autoScrollStep is zero in the middle, ramps near each edge, is capped at max, and handles degenerate boxes', () => {
    const box = { start: 0, end: 400 }
    expect(autoScrollStep(box, 200)).toBe(0)
    expect(autoScrollStep(box, 48)).toBe(0)
    expect(autoScrollStep(box, 352)).toBe(0)
    expect(autoScrollStep(box, 24)).toBeCloseTo(-4)
    expect(autoScrollStep(box, 376)).toBeCloseTo(4)
    expect(autoScrollStep(box, 12)).toBeLessThan(autoScrollStep(box, 24))
    expect(autoScrollStep(box, 0)).toBe(-16)
    expect(autoScrollStep(box, 400)).toBe(16)
    expect(autoScrollStep(box, 10, 48, 8)).toBeGreaterThanOrEqual(-8)
    // Outside the box.
    expect(autoScrollStep(box, -5)).toBe(0)
    expect(autoScrollStep(box, 405)).toBe(0)
    // Degenerate boxes.
    expect(autoScrollStep({ start: 10, end: 10 }, 10)).toBe(0)
    expect(autoScrollStep({ start: 50, end: 10 }, 20)).toBe(0)
    // A box narrower than two bands shrinks the band instead of overlapping.
    expect(autoScrollStep({ start: 0, end: 40 }, 20)).toBe(0)
    expect(autoScrollStep({ start: 0, end: 40 }, 5)).toBeLessThan(0)
    expect(autoScrollStep({ start: 0, end: 40 }, 35)).toBeGreaterThan(0)
  })

  it('exceedsThreshold', () => {
    expect(exceedsThreshold(0, 0)).toBe(false)
    expect(exceedsThreshold(3, 4)).toBe(false)
    expect(exceedsThreshold(4, 4)).toBe(true)
    expect(exceedsThreshold(-6, 0)).toBe(true)
    expect(exceedsThreshold(0, 2, 1)).toBe(true)
  })

  it('moveAnnouncement strings', () => {
    expect(moveAnnouncement('pickup', 'Ship it', 'To do', 2, 5)).toBe(
      'Picked up Ship it, position 2 of 5 in To do.',
    )
    expect(moveAnnouncement('drop', 'Ship it', 'In progress', 3, 5)).toBe(
      'Dropped Ship it at position 3 of 5 in In progress.',
    )
    expect(moveAnnouncement('cancel', 'Ship it', 'To do', 2, 5)).toBe(
      'Move cancelled. Ship it is back at position 2 of 5 in To do.',
    )
    expect(moveAnnouncement('fail', 'Ship it', 'To do', 2, 5)).toBe('Could not save the new order.')
  })
})
