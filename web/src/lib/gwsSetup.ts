import type { GwsIntegrationProfile } from './types'

export type GwsSetupStepState = 'done' | 'current' | 'pending'

export type GwsDialogStepId = 'install' | 'client' | 'signin'
export interface GwsDialogStep { id: GwsDialogStepId; state: GwsSetupStepState }

const DIALOG_STEP_ORDER: GwsDialogStepId[] = ['install', 'client', 'signin']

/**
 * The Connect dialog's checklist for ONE profile (null = not created yet).
 * The first undone step is 'current', later ones 'pending'.
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
