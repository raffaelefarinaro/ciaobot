// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { safeReturnPath } from '../auth'

describe('safeReturnPath', () => {
  it('accepts an internal path, including the onboarding destination', () => {
    expect(safeReturnPath('/settings/workspaces')).toBe('/settings/workspaces')
    expect(safeReturnPath('/onboarding?workspace=studio')).toBe('/onboarding?workspace=studio')
  })

  it('rejects external, protocol-relative and backslash destinations', () => {
    expect(safeReturnPath('https://evil.example/')).toBeNull()
    expect(safeReturnPath('//evil.example/')).toBeNull()
    expect(safeReturnPath('/\\evil.example')).toBeNull()
  })

  it('rejects a destination whose normalization escapes this origin', () => {
    // A raw tab is normalized by the browser into an absolute external URL;
    // a percent-encoded one stays inside the path and is safe.
    expect(safeReturnPath('/\t/evil.example')).toBeNull()
    expect(safeReturnPath('/%09/evil.example')).toBe('/%09/evil.example')
  })

  it('rejects login loops and setup-token destinations', () => {
    expect(safeReturnPath('/login')).toBeNull()
    expect(safeReturnPath('/login?returnTo=/x')).toBeNull()
    expect(safeReturnPath('/settings?setup_token=abc')).toBeNull()
    expect(safeReturnPath(null)).toBeNull()
  })
})
