// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SW_READY_TIMEOUT_MS, currentSubscription } from './push'

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('getRegistration', () => {
  it('gives up on a service worker that never becomes ready', async () => {
    // A browser with no registration has no push to read, and
    // `navigator.serviceWorker.ready` never settles in that state. Every caller
    // is UI state (the Home setup card, the Settings notifications card), so an
    // unbounded wait froze both of them.
    vi.stubGlobal('navigator', {
      serviceWorker: {
        getRegistration: async () => undefined,
        ready: new Promise(() => {}),
      },
    })

    const pending = currentSubscription()
    await vi.advanceTimersByTimeAsync(SW_READY_TIMEOUT_MS)

    await expect(pending).resolves.toBeNull()
  })
})
