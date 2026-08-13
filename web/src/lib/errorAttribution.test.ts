import { describe, expect, it } from 'vitest'
import { classifyError } from './errorAttribution'

describe('classifyError', () => {
  it.each([
    ['request timed out after 30s', 'timeout'],
    ['permission denied by policy', 'blocked'],
    ['HTTP 502 from gateway', 'remote-http'],
    ['Anthropic quota exceeded', 'provider'],
    ['something unexpected', 'unknown'],
  ])('classifies %s as %s', (text, kind) => {
    expect(classifyError(text).kind).toBe(kind)
  })
})
