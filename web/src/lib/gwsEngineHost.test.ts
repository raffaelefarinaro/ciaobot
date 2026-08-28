import { describe, expect, test } from 'vitest'
import { isGwsEngineHostEligible, type GwsNodeStatusKnowledge } from './gwsEngineHost'

function status(overrides: Partial<GwsNodeStatusKnowledge> = {}): GwsNodeStatusKnowledge {
  return { loaded: true, error: false, isClient: false, ...overrides }
}

describe('isGwsEngineHostEligible (issue #351)', () => {
  test('fetch failure off a loopback hostname is ineligible, not fail-open', () => {
    expect(
      isGwsEngineHostEligible('192.168.1.20', false, status({ loaded: true, error: true, isClient: false })),
    ).toBe(false)
  })

  test('fetch failure on localhost stays eligible (single-machine case)', () => {
    expect(
      isGwsEngineHostEligible('localhost', false, status({ loaded: true, error: true, isClient: false })),
    ).toBe(true)
  })

  test('still loading, off a loopback hostname, is ineligible', () => {
    expect(
      isGwsEngineHostEligible('phone.lan', false, status({ loaded: false, error: false, isClient: false })),
    ).toBe(false)
  })

  test('still loading, on localhost, stays eligible', () => {
    expect(
      isGwsEngineHostEligible('localhost', false, status({ loaded: false, error: false, isClient: false })),
    ).toBe(true)
  })

  test('a confirmed client role is ineligible even on localhost', () => {
    expect(
      isGwsEngineHostEligible('localhost', false, status({ loaded: true, error: false, isClient: true })),
    ).toBe(false)
  })

  test('a confirmed host role on localhost is eligible', () => {
    expect(
      isGwsEngineHostEligible('127.0.0.1', false, status({ loaded: true, error: false, isClient: false })),
    ).toBe(true)
  })

  test('a confirmed host role off localhost is ineligible (LAN browser)', () => {
    expect(
      isGwsEngineHostEligible('192.168.1.20', false, status({ loaded: true, error: false, isClient: false })),
    ).toBe(false)
  })

  test('desktop app while still loading is eligible without a loopback check', () => {
    expect(
      isGwsEngineHostEligible('localhost', true, status({ loaded: false, error: false, isClient: false })),
    ).toBe(true)
  })

  test('desktop app with a confirmed client role is still ineligible', () => {
    expect(
      isGwsEngineHostEligible('localhost', true, status({ loaded: true, error: false, isClient: true })),
    ).toBe(false)
  })

  test('desktop app off a loopback hostname with unresolved status is ineligible', () => {
    expect(
      isGwsEngineHostEligible('192.168.1.20', true, status({ loaded: true, error: true, isClient: false })),
    ).toBe(false)
  })
})
