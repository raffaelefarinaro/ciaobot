// @vitest-environment jsdom

import { describe, expect, it, beforeEach } from 'vitest'
import {
  PRODUCT_TOUR_STEPS,
  TOUR_STORAGE_KEY,
  clearTourCompleted,
  isTourCompleted,
  markTourCompleted,
  shouldShowMissingHint,
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

  it('offers "Try it" deep links into the real pages', () => {
    const withActions = Object.fromEntries(
      PRODUCT_TOUR_STEPS.filter(s => s.action).map(s => [s.id, s.action!.route]),
    )
    expect(withActions['workspaces']).toBe('/settings/workspaces')
    expect(withActions['schedules']).toBe('/schedules')
    for (const step of PRODUCT_TOUR_STEPS) {
      if (!step.action) continue
      expect(step.action.route.startsWith('/')).toBe(true)
      expect(step.action.label.length).toBeGreaterThan(0)
    }
  })

  it('shows missing hints when chat UI or projects are unavailable', () => {
    const modelStep = PRODUCT_TOUR_STEPS.find(s => s.id === 'model')!
    const fileStep = PRODUCT_TOUR_STEPS.find(s => s.id === 'file-cards')!
    const projectsStep = PRODUCT_TOUR_STEPS.find(s => s.id === 'projects')!

    expect(shouldShowMissingHint(modelStep, { hasActiveChat: false, projectCount: 1 }, false)).toBe(true)
    expect(shouldShowMissingHint(modelStep, { hasActiveChat: true, projectCount: 1 }, true)).toBe(false)
    expect(shouldShowMissingHint(fileStep, { hasActiveChat: false, projectCount: 1 }, true)).toBe(true)
    expect(shouldShowMissingHint(fileStep, { hasActiveChat: true, projectCount: 1 }, true)).toBe(false)
    expect(shouldShowMissingHint(projectsStep, { hasActiveChat: true, projectCount: 0 }, true)).toBe(true)
    expect(shouldShowMissingHint(projectsStep, { hasActiveChat: true, projectCount: 2 }, true)).toBe(false)
  })
})
