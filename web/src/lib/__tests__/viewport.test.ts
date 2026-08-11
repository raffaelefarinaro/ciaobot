// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { installViewportPlumbing, stopViewportPlumbing, viewportHeight } from '../viewport'

afterEach(() => {
  stopViewportPlumbing()
  vi.useRealTimers()
})

describe('viewport plumbing', () => {
  it('sets --app-h from the visible viewport', () => {
    installViewportPlumbing()
    expect(document.documentElement.style.getPropertyValue('--app-h'))
      .toBe(`${viewportHeight()}px`)
  })

  // The settling timers fire up to 500ms out. Under vitest that can land after
  // the jsdom environment owning `window` is gone, which failed unrelated test
  // files with "ReferenceError: window is not defined".
  it('does not throw when a settling timer fires without a window', () => {
    vi.useFakeTimers()
    installViewportPlumbing()

    const realWindow = globalThis.window
    // Simulate teardown: the timer is already scheduled, the global is gone.
    delete (globalThis as { window?: unknown }).window
    try {
      expect(() => vi.advanceTimersByTime(600)).not.toThrow()
    } finally {
      globalThis.window = realWindow
    }
  })

  it('cancels pending timers so repeated installs cannot pile up', () => {
    vi.useFakeTimers()
    installViewportPlumbing()
    installViewportPlumbing()
    installViewportPlumbing()
    // Three sets scheduled, two cleared: only one set may remain.
    expect(vi.getTimerCount()).toBeLessThanOrEqual(3)
    stopViewportPlumbing()
    expect(vi.getTimerCount()).toBe(0)
  })
})
