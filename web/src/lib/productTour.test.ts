// @vitest-environment jsdom

import { describe, expect, it, beforeEach } from 'vitest'
import {
  PRODUCT_TOUR_STEPS,
  TOUR_STORAGE_KEY,
  clearTourCompleted,
  isTourCompleted,
  markTourCompleted,
  tourTargetSelector,
} from './productTour'

describe('productTour', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('defines a multi-step tour with chat and file flows', () => {
    expect(PRODUCT_TOUR_STEPS.length).toBeGreaterThanOrEqual(8)
    const ids = PRODUCT_TOUR_STEPS.map(s => s.id)
    expect(ids).toContain('chat-comments')
    expect(ids).toContain('file-cards')
    expect(ids).toContain('pin-preview')
    expect(ids).toContain('rich-preview')
  })

  it('tracks completion in localStorage', () => {
    expect(isTourCompleted()).toBe(false)
    markTourCompleted()
    expect(isTourCompleted()).toBe(true)
    expect(localStorage.getItem(TOUR_STORAGE_KEY)).toBe('1')
    clearTourCompleted()
    expect(isTourCompleted()).toBe(false)
  })

  it('builds data-tour selectors', () => {
    expect(tourTargetSelector('chat-input')).toBe('[data-tour="chat-input"]')
  })
})
