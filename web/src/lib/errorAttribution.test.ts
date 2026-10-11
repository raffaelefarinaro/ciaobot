import { describe, expect, it } from 'vitest'
import { classifyError, isProviderAuthError, isUsageLimitError } from './errorAttribution'

const OLLAMA_WEEKLY = 'you (lozioraf) have reached your weekly usage limit, upgrade for higher limits: https://ollama.com/upgrade or add usage credits: https://ollama.com/settings (ref: c95e827a-1234)'

describe('classifyError', () => {
  it.each([
    ['Failed to authenticate: OAuth session expired and could not be refreshed', 'auth'],
    ['Not logged in · Please run /login', 'auth'],
    ['request timed out after 30s', 'timeout'],
    ['permission denied by policy', 'blocked'],
    ['HTTP 502 from gateway', 'remote-http'],
    ['Anthropic quota exceeded', 'quota'],
    [OLLAMA_WEEKLY, 'quota'],
    ["API Error: Request rejected (429) · you (x) have reached your weekly usage limit, upgrade for higher limits: https://ollama.com/upgrade", 'quota'],
    ["You've hit your session limit · resets 3pm", 'quota'],
    ['Rate limit exceeded, retry later', 'quota'],
    ['OpenCode model not found', 'provider'],
    ['something unexpected', 'unknown'],
  ])('classifies %s as %s', (text, kind) => {
    expect(classifyError(text).kind).toBe(kind)
  })
})

describe('usage-limit copy', () => {
  it('tells the user a weekly limit will not reset soon', () => {
    const attribution = classifyError(OLLAMA_WEEKLY)
    expect(attribution.label).toBe('Usage limit reached')
    expect(attribution.copy).toContain('won\'t reset for a while')
  })

  it('keeps the short-limit copy for a session limit', () => {
    const attribution = classifyError('You\'ve hit your session limit')
    expect(attribution.copy).toContain('says when it resets')
    expect(attribution.copy).not.toContain('won\'t reset')
  })

  it('exports isUsageLimitError', () => {
    expect(isUsageLimitError(OLLAMA_WEEKLY)).toBe(true)
    expect(isUsageLimitError('something unexpected')).toBe(false)
  })
})

describe('isProviderAuthError', () => {
  it('does not treat every authentication failure as a lapsed login', () => {
    // A revoked API key needs a different fix than signing in again.
    expect(isProviderAuthError('Failed to authenticate: invalid x-api-key')).toBe(false)
    expect(isProviderAuthError('Error: OAuth session expired')).toBe(true)
  })
})
