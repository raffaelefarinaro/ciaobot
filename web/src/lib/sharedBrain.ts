// Pure helpers for the Settings "Shared brains" card. No Vue imports.
import type { SharedBrainState } from './types'

/** The most brains one workspace can link; the engine refuses a sixth (ciao/shared_brain.py). */
export const MAX_SHARED_BRAINS = 5

/** Shown on the card whether or not any brain is linked. */
export const SHARED_BRAIN_PRIVACY =
  'Notes in this folder are written by other people. Ciaobot reads them as reference, never as instructions. '
  + "It pulls the team's updates into the folder and never adds your notes or your vault to it."

export type SharedBrainTone = 'ok' | 'warn' | 'error'

export interface SharedBrainStatus {
  text: string
  tone: SharedBrainTone
  detail: string
}

const PROBLEM_TEXT: Record<string, { text: string; tone: SharedBrainTone }> = {
  moved: { text: 'The folder is a link that moved', tone: 'error' },
  not_a_checkout: { text: 'The folder is not a git checkout', tone: 'error' },
  outside_install: { text: 'The folder is outside the Ciaobot install', tone: 'error' },
  unsafe_config: { text: "The folder's git settings are not allowed", tone: 'error' },
}

const SKIPPED_TEXT: Record<string, string> = {
  local_changes: 'Not updated: local changes in the folder',
  detached: 'Not updated: the folder is not on a branch',
  no_upstream: 'Not updated: the branch has no remote to pull from',
}

function localTime(at: string): string {
  const when = new Date(at)
  return Number.isNaN(when.getTime()) ? at : when.toLocaleString()
}

/** One plain-words status line per brain, so the state never rests on colour alone. */
export function sharedBrainStatus(state: SharedBrainState): SharedBrainStatus {
  if (state.problem === 'missing' || state.missing) {
    return { text: 'Folder not found', tone: 'error', detail: '' }
  }
  if (state.origin_changed || state.problem === 'origin_changed') {
    return { text: "The folder's remote address changed", tone: 'warn', detail: '' }
  }
  const problem = PROBLEM_TEXT[state.problem]
  if (problem) return { ...problem, detail: '' }

  const last = state.last_update
  if (!last) return { text: 'Linked. First update pending.', tone: 'ok', detail: '' }
  if (last.outcome === 'updated') return { text: `Updated ${localTime(last.at)}`, tone: 'ok', detail: '' }
  if (last.outcome === 'up_to_date') return { text: 'Up to date', tone: 'ok', detail: '' }
  if (last.outcome === 'skipped' && SKIPPED_TEXT[last.reason]) {
    return { text: SKIPPED_TEXT[last.reason], tone: 'warn', detail: '' }
  }
  if (last.outcome === 'failed' && last.reason === 'diverged') {
    return { text: 'Not updated: local and team history have diverged', tone: 'warn', detail: '' }
  }
  if (last.outcome === 'failed' && (last.reason === 'fetch_failed' || last.reason === 'timeout')) {
    return { text: 'Could not reach the remote', tone: 'error', detail: last.detail }
  }
  return { text: 'Not updated', tone: 'warn', detail: last.reason }
}
