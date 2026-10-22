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
export interface GwsSetupGroups {
  /** The steps already finished, in setup order. */
  done: GwsSetupStep[]
  /** The one step to do next; null when no step is current. */
  current: GwsSetupStep | null
  /** The steps that are neither finished nor the current one, in setup order. */
  pending: GwsSetupStep[]
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

/**
 * Groups the derived steps into what is finished, the one step to do next and
 * the steps that follow it. The setup order is preserved in every group; the
 * derivation itself is untouched.
 */
export function gwsSetupGroups(progress: GwsSetupProgress): GwsSetupGroups {
  const done: GwsSetupStep[] = []
  const pending: GwsSetupStep[] = []
  let current: GwsSetupStep | null = null
  for (const step of progress.steps) {
    if (step.state === 'done') {
      done.push(step)
    } else if (step.state === 'current' && !current) {
      current = step
    } else {
      pending.push(step)
    }
  }
  return { done, current, pending }
}

export type GwsDialogStepId = 'install' | 'client' | 'signin'
export interface GwsDialogStep { id: GwsDialogStepId; state: GwsSetupStepState }

const DIALOG_STEP_ORDER: GwsDialogStepId[] = ['install', 'client', 'signin']

/**
 * The Connect dialog's checklist for ONE profile (null = not created yet).
 * Same rule as gwsSetupProgress: the first undone step is 'current', later
 * ones 'pending'.
 */
export function gwsDialogSteps(s: { installed: boolean }, p: GwsIntegrationProfile | null): GwsDialogStep[] {
  const done: Record<GwsDialogStepId, boolean> = {
    install: s.installed,
    client: !!p && (p.client_secret_present || p.configured),
    signin: !!p && p.configured && !p.needs_relogin,
  }
  let currentFound = false
  return DIALOG_STEP_ORDER.map((id) => {
    if (done[id]) return { id, state: 'done' }
    if (!currentFound) {
      currentFound = true
      return { id, state: 'current' }
    }
    return { id, state: 'pending' }
  })
}

export type GwsAccountState = 'connected' | 'expired' | 'none'

/**
 * configured && !needs_relogin -> connected; configured && needs_relogin ->
 * expired; anything else (no profile, no credentials) -> none.
 */
export function gwsAccountState(p: GwsIntegrationProfile | null | undefined): GwsAccountState {
  if (!p || !p.configured) return 'none'
  return p.needs_relogin ? 'expired' : 'connected'
}
