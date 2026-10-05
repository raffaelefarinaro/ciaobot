import { describe, expect, it } from 'vitest'
import { classifyError, isProviderAuthError } from './errorAttribution'

describe('classifyError', () => {
  it.each([
    ['Failed to authenticate: OAuth session expired and could not be refreshed', 'auth'],
    ['Not logged in · Please run /login', 'auth'],
    ['request timed out after 30s', 'timeout'],
    ['permission denied by policy', 'blocked'],
    ['HTTP 502 from gateway', 'remote-http'],
    ['Anthropic quota exceeded', 'provider'],
    ['something unexpected', 'unknown'],
  ])('classifies %s as %s', (text, kind) => {
    expect(classifyError(text).kind).toBe(kind)
  })
})

describe('isProviderAuthError', () => {
  it('does not treat every authentication failure as a lapsed login', () => {
    // A revoked API key needs a different fix than signing in again.
    expect(isProviderAuthError('Failed to authenticate: invalid x-api-key')).toBe(false)
    expect(isProviderAuthError('Error: OAuth session expired')).toBe(true)
  })
})
