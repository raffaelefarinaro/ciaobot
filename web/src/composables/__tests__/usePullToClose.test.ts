// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PULL_CLOSE_PX, pullCloses, usePullToClose } from '../usePullToClose'

function setReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({ matches: reduce && q.includes('reduce'), media: q })) as never
}

describe('pullCloses', () => {
  it('closes past the threshold or on a downward flick', () => {
    expect(pullCloses(PULL_CLOSE_PX, 0)).toBe(true)
    expect(pullCloses(PULL_CLOSE_PX - 1, 0)).toBe(false)
    expect(pullCloses(50, 0.7)).toBe(true)
    expect(pullCloses(30, 2)).toBe(false)
  })
})

describe('usePullToClose', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setReducedMotion(false)
  })
  afterEach(() => vi.useRealTimers())

  it('follows the finger, then drops away and closes past the threshold', () => {
    const close = vi.fn()
    const pull = usePullToClose(close)
    pull.onPull(60)
    expect(pull.pullY.value).toBe(60)
    expect(pull.pullStyle.value?.transform).toContain('translateY(60px)')
    pull.onPullEnd(PULL_CLOSE_PX + 10, 0.1)
    expect(close).not.toHaveBeenCalled()
    vi.advanceTimersByTime(280)
    expect(close).toHaveBeenCalledTimes(1)
    expect(pull.pullY.value).toBe(0)
  })

  it('springs back short of the threshold', () => {
    const close = vi.fn()
    const pull = usePullToClose(close)
    pull.onPull(50)
    pull.onPullEnd(50, 0.1)
    vi.advanceTimersByTime(400)
    expect(close).not.toHaveBeenCalled()
    expect(pull.pullStyle.value).toBeUndefined()
  })

  it('closes at once under reduced motion', () => {
    setReducedMotion(true)
    const close = vi.fn()
    const pull = usePullToClose(close)
    pull.onPullEnd(PULL_CLOSE_PX, 0)
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('a pull invalidated mid-settle never closes', () => {
    const close = vi.fn()
    const pull = usePullToClose(close)
    pull.onPullEnd(PULL_CLOSE_PX + 1, 0)
    pull.invalidate()
    vi.advanceTimersByTime(400)
    expect(close).not.toHaveBeenCalled()
  })

  it('does nothing while disabled', () => {
    const close = vi.fn()
    const pull = usePullToClose(close, () => true)
    pull.onPull(200)
    pull.onPullEnd(200, 1)
    vi.advanceTimersByTime(400)
    expect(pull.pullY.value).toBe(0)
    expect(close).not.toHaveBeenCalled()
  })
})
