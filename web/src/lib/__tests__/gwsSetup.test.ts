import { describe, expect, it } from 'vitest'
import { gwsSetupProgress } from '../gwsSetup'
import type { GwsIntegrationProfile, GwsIntegrationSettings } from '../types'

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

function integration(profiles: GwsIntegrationProfile[], installed = true): GwsIntegrationSettings {
  return {
    installed,
    binary_path: '/usr/local/bin/gws',
    default_profile: 'personal',
    cli_available: true,
    profiles,
  }
}

function states(s: GwsIntegrationSettings): Record<string, string> {
  return Object.fromEntries(gwsSetupProgress(s).steps.map((step) => [step.id, step.state]))
}

describe('gwsSetupProgress', () => {
  it('starts at install when gws is missing', () => {
    const progress = gwsSetupProgress(integration([], false))
    expect(progress.focus).toBeNull()
    expect(progress.complete).toBe(false)
    expect(states(integration([], false))).toEqual({
      install: 'current',
      account: 'pending',
      client: 'pending',
      signin: 'pending',
      link: 'pending',
    })
  })

  it('asks for an account when none exist', () => {
    const s = integration([])
    expect(states(s)).toEqual({
      install: 'done',
      account: 'current',
      client: 'pending',
      signin: 'pending',
      link: 'pending',
    })
    expect(gwsSetupProgress(s).complete).toBe(false)
  })

  it('asks for a client before sign-in', () => {
    const s = integration([profile({ client_secret_present: false, configured: false })])
    expect(states(s)).toMatchObject({ client: 'current', signin: 'pending' })
  })

  it('asks for sign-in once a client exists', () => {
    const s = integration([profile({ client_secret_present: true, configured: false })])
    expect(states(s)).toMatchObject({ client: 'done', signin: 'current' })
  })

  it('treats an expired login as not signed in', () => {
    const s = integration([profile({ configured: true, needs_relogin: true })])
    expect(states(s)).toMatchObject({ signin: 'current' })
  })

  it('asks to link a workspace after sign-in', () => {
    const s = integration([profile({ workspaces: [] })])
    expect(states(s)).toMatchObject({ signin: 'done', link: 'current' })
  })

  it('focuses the first profile that is not ready', () => {
    const s = integration([
      profile(),
      profile({ name: 'work', label: 'Work Google account', client_secret_present: false, configured: false }),
    ])
    const progress = gwsSetupProgress(s)
    expect(progress.focus?.name).toBe('work')
    expect(states(s)).toMatchObject({ client: 'current' })
  })

  it('is complete when every profile is signed in and linked', () => {
    const progress = gwsSetupProgress(integration([profile(), profile({ name: 'work', workspaces: ['work'] })]))
    expect(progress.complete).toBe(true)
    expect(progress.focus).toBeNull()
    expect(progress.steps.every((step) => step.state === 'done')).toBe(true)
  })

  it('keeps ready accounts done while gws itself is missing', () => {
    const progress = gwsSetupProgress(integration([profile()], false))
    expect(progress.complete).toBe(false)
    expect(states(integration([profile()], false))).toEqual({
      install: 'current',
      account: 'done',
      client: 'done',
      signin: 'done',
      link: 'done',
    })
  })
})
