import { describe, expect, it } from 'vitest'
import { classifyError, errorQuickAction, isProviderAuthError, isUsageLimitError } from './errorAttribution'

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
    ['disk quota exceeded', 'unknown'],
    ['Google API quota exceeded for quota metric generativelanguage.googleapis.com', 'unknown'],
    ['something unexpected', 'unknown'],
  ])('classifies %s as %s', (text, kind) => {
    expect(classifyError(text).kind).toBe(kind)
  })
})

describe('isUsageLimitError', () => {
  it.each([
    'disk quota exceeded',
    'Google API quota exceeded for quota metric generativelanguage.googleapis.com',
    '429 Too Many Requests',
  ])('does not match tool quota text: %s', (text) => {
    expect(isUsageLimitError(text)).toBe(false)
  })

  it.each([
    'Rate limit exceeded, retry later',
    'Anthropic quota exceeded',
    'The usage limit has been reached',
    'Rate limit exhausted for this key',
    'Anthropic quota exhausted',
    'API Error: 429 Too Many Requests: rate limit',
    'HTTP 429: quota exceeded for this session',
  ])('matches provider usage-limit phrasing: %s', (text) => {
    expect(isUsageLimitError(text)).toBe(true)
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

  it('classifies a 429 usage limit as quota, not a remote HTTP error', () => {
    // The status 4xx rule would otherwise match first.
    expect(classifyError('HTTP 429: quota exceeded for this session').kind).toBe('quota')
    expect(classifyError('API Error: 429 Too Many Requests: rate limit').kind).toBe('quota')
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

describe('errorQuickAction', () => {
  it.each([
    ['Not logged in · Please run /login', 'sign-in'],
    [OLLAMA_WEEKLY, 'retry-hourly'],
    ['Rate limit exceeded, retry later', 'retry-hourly'],
    ['OpenCode model not found', 'switch-model'],
    ['Error: this model does not support image input', 'switch-model'],
    ['request timed out after 30s', 'fix'],
    ['disk quota exceeded', 'fix'],
    ['something unexpected', 'fix'],
  ])('maps %s to %s', (text, action) => {
    expect(errorQuickAction(text)).toBe(action)
  })
})
