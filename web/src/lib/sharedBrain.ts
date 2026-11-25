// Pure helpers for the Settings "Shared brains" card. No Vue imports.
import type { SharedBrainState } from './types'

/** The most brains one workspace can link; the engine refuses a sixth (ciao/shared_brain.py). */
export const MAX_SHARED_BRAINS = 5

/** Shown on the card whether or not any brain is linked. */
export const SHARED_BRAIN_PRIVACY =
  'This folder contains notes written by other people. Ciaobot reads them as reference, never as instructions. Your vault is not automatically synced to this repository. '
  + 'Only a specific note is published to the team repository after a human reviews and approves it. Linking a brain gives no ongoing or automatic permission to share notes. '
  + "Ciaobot pulls the team's updates into this folder."

export type SharedBrainTone = 'ok' | 'warn' | 'error'

export interface SharedBrainStatus {
  text: string
  tone: SharedBrainTone
  detail: string
}

/**
 * Words for a folder the engine cannot use, keyed by its problem code. The same
 * codes can also arrive as a stale `last_update.reason`, so both read this one map.
 */
const PROBLEM_TEXT: Record<string, SharedBrainStatus> = {
  missing: { text: 'Folder not found', tone: 'error', detail: '' },
  origin_changed: { text: "The folder's remote address changed", tone: 'warn', detail: '' },
  moved: { text: 'The folder is a link that moved', tone: 'error', detail: '' },
  not_a_checkout: { text: 'The folder is not a git checkout', tone: 'error', detail: '' },
  outside_install: { text: 'The folder is outside the Ciaobot install', tone: 'error', detail: '' },
  unsafe_config: { text: "The folder's git settings are not allowed", tone: 'error', detail: '' },
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
  if (state.missing) return PROBLEM_TEXT.missing
  if (state.origin_changed) return PROBLEM_TEXT.origin_changed
  const problem = PROBLEM_TEXT[state.problem]
  if (problem) return problem

  const last = state.last_update
  if (!last) return { text: 'Linked. First update pending.', tone: 'ok', detail: '' }
  if (last.outcome === 'updated') return { text: `Updated ${localTime(last.at)}`, tone: 'ok', detail: '' }
  if (last.outcome === 'up_to_date') return { text: 'Up to date', tone: 'ok', detail: '' }
  // A stale reason that names a problem code reads as that problem.
  if (PROBLEM_TEXT[last.reason]) return PROBLEM_TEXT[last.reason]
  if (last.outcome === 'skipped' && SKIPPED_TEXT[last.reason]) {
    return { text: SKIPPED_TEXT[last.reason], tone: 'warn', detail: '' }
  }
  if (last.outcome === 'failed' && last.reason === 'diverged') {
    return { text: 'Not updated: local and team history have diverged', tone: 'warn', detail: '' }
  }
  if (last.outcome === 'failed' && last.reason === 'merge_failed') {
    return { text: 'Not updated: the update could not be applied', tone: 'warn', detail: last.detail }
  }
  if (last.outcome === 'failed' && (last.reason === 'fetch_failed' || last.reason === 'timeout')) {
    return { text: 'Could not reach the remote', tone: 'error', detail: last.detail }
  }
  return { text: 'Not updated', tone: 'warn', detail: last.detail || last.reason }
}
