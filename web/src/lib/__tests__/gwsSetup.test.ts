import { describe, expect, it } from 'vitest'
import { gwsAccountState, gwsDialogSteps } from '../gwsSetup'
import type { GwsIntegrationProfile } from '../types'

function profile(overrides: Partial<GwsIntegrationProfile> = {}): GwsIntegrationProfile {
  return {
    name: 'personal',
    label: 'Personal Google account',
    purpose: 'Personal mail, calendar and drive.',
    examples: ['gmail', 'calendar'],
    configured: true,
    credentials_present: true,
    client_secret_present: true,
    config_dir: '/tmp/secrets/gws-personal',
    workspaces: ['personal'],
    setup_command: 'ciao gws personal auth login --full',
    headless_auth_command: 'ciao gws-auth-helper personal',
    email: 'someone@example.com',
    token_valid: true,
    token_error: '',
    needs_relogin: false,
    loopback_eligible: true,
    ...overrides,
  }
}

describe('gwsDialogSteps', () => {
  const states = (installed: boolean, p: GwsIntegrationProfile | null) =>
    gwsDialogSteps({ installed }, p).map((step) => step.state)

  it('marks the first undone step current', () => {
    expect(states(true, null)).toEqual(['done', 'current', 'pending'])
    expect(states(true, profile({ configured: false, client_secret_present: true }))).toEqual(['done', 'done', 'current'])
    expect(states(true, profile())).toEqual(['done', 'done', 'done'])
    expect(states(false, profile())).toEqual(['current', 'done', 'done'])
  })
})

describe('gwsAccountState', () => {
  it('maps a profile to the row state', () => {
    expect(gwsAccountState(null)).toBe('none')
    expect(gwsAccountState(undefined)).toBe('none')
    expect(gwsAccountState(profile({ configured: false, client_secret_present: true }))).toBe('none')
    expect(gwsAccountState(profile())).toBe('connected')
    expect(gwsAccountState(profile({ needs_relogin: true }))).toBe('expired')
  })
})
