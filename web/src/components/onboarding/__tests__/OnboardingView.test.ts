// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { routes } from '../../../router'

describe('onboarding route', () => {
  it('exposes an authenticated onboarding page', () => {
    expect(routes.find(route => route.path === '/onboarding')?.meta?.requiresAuth).toBe(true)
  })
})
