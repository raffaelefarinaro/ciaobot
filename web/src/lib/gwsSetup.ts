import type { GwsIntegrationProfile, GwsIntegrationSettings } from './types'

export type GwsSetupStepId = 'install' | 'account' | 'client' | 'signin' | 'link'
export type GwsSetupStepState = 'done' | 'current' | 'pending'
export interface GwsSetupStep { id: GwsSetupStepId; state: GwsSetupStepState }
export interface GwsSetupProgress {
  /** The profile the steps describe; null before any account exists. */
  focus: GwsIntegrationProfile | null
  steps: GwsSetupStep[]
  /** True when every profile is signed in, not expired, and linked. */
  complete: boolean
}

const STEP_ORDER: GwsSetupStepId[] = ['install', 'account', 'client', 'signin', 'link']

/** A profile is ready when it is signed in, its login has not expired, and at least one workspace uses it. */
export function gwsProfileReady(p: GwsIntegrationProfile): boolean {
  return p.configured && !p.needs_relogin && p.workspaces.length > 0
}

/**
 * Derives the Google Workspace setup checklist from the integration payload.
 * It follows one focus profile: the first profile that is not yet ready.
 */
export function gwsSetupProgress(s: GwsIntegrationSettings): GwsSetupProgress {
  const focus = s.profiles.find((p) => !gwsProfileReady(p)) ?? null
  // Every account ready: the per-account steps are done even while gws itself is missing.
  const allReady = s.profiles.length > 0 && focus === null
  const complete = s.installed && allReady

  const done: Record<GwsSetupStepId, boolean> = {
    install: s.installed,
    account: s.profiles.length > 0,
    client: allReady || (!!focus && (focus.client_secret_present || focus.configured)),
    signin: allReady || (!!focus && focus.configured && !focus.needs_relogin),
    link: allReady || (!!focus && focus.workspaces.length > 0),
  }

  let currentFound = false
  const steps: GwsSetupStep[] = STEP_ORDER.map((id) => {
    if (complete || done[id]) return { id, state: 'done' }
    if (!currentFound) {
      currentFound = true
      return { id, state: 'current' }
    }
    return { id, state: 'pending' }
  })

  return { focus, steps, complete }
}
