import { describe, it, expect } from 'vitest'
import { errorMessage } from './errorMessage'

// Each case pins the behaviour of the `e?.message || <fallback>` expression
// this helper replaced, so the sweep that introduced it cannot change what a
// user sees in an error banner.
describe('errorMessage', () => {
  it('prefers a truthy message property', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom')
    expect(errorMessage({ message: 'nope' }, 'fallback')).toBe('nope')
  })

  it('falls back for an empty or missing message', () => {
    expect(errorMessage({ message: '' }, 'fallback')).toBe('fallback')
    expect(errorMessage({}, 'fallback')).toBe('fallback')
  })

  it('stringifies the value itself when no fallback is given', () => {
    expect(errorMessage('boom')).toBe('boom')
    expect(errorMessage(null)).toBe('null')
    expect(errorMessage(undefined)).toBe('undefined')
    expect(errorMessage(404)).toBe('404')
  })

  it('coerces a non-string message', () => {
    expect(errorMessage({ message: 500 })).toBe('500')
  })
})
