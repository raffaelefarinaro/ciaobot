// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProductTourStore } from './productTour'
import { clearTourCompleted } from '../lib/productTour'

describe('productTour store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('auto-starts only when the tour was not completed', async () => {
    const tour = useProductTourStore()
    const openSidebar = vi.fn()
    tour.registerHooks({ openSidebar, navigateToChat: vi.fn(), ensureWelcomeChat: vi.fn() })

    await tour.maybeAutoStart()
    expect(tour.active).toBe(true)
    tour.finish()

    await tour.maybeAutoStart()
    expect(tour.active).toBe(false)
  })

  it('replay clears completion and restarts', async () => {
    const tour = useProductTourStore()
    tour.registerHooks({
      openSidebar: vi.fn(),
      navigateToChat: vi.fn(),
      ensureWelcomeChat: vi.fn(),
    })
    tour.finish()
    expect(tour.isTourCompleted()).toBe(true)

    clearTourCompleted()
    await tour.replay()
    expect(tour.active).toBe(true)
    expect(tour.stepIndex).toBe(0)
  })

  it('runs beforeEnter hooks when advancing', async () => {
    const tour = useProductTourStore()
    const openSidebar = vi.fn()
    tour.registerHooks({
      openSidebar,
      navigateToChat: vi.fn(),
      ensureWelcomeChat: vi.fn(),
    })
    await tour.start(true)
    await tour.next()
    expect(openSidebar).toHaveBeenCalled()
  })
})
