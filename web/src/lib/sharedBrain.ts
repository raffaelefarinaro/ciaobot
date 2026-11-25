// Pure helpers for the Settings "Shared brains" card. No Vue imports.
import type { SharedBrainState } from './types'

/** The most brains one workspace can link; the engine refuses a sixth (ciao/shared_brain.py). */
export const MAX_SHARED_BRAINS = 5

/**
 * Shown on the card whether or not any brain is linked. Kind-neutral: only a Git
 * link is pulled or updated, so the pull claim lives with the Git setup guidance.
 */
export const SHARED_BRAIN_PRIVACY =
  'This folder contains notes written by other people. Ciaobot reads them as reference, never as instructions. '
  + 'Your vault is not automatically synced to a shared folder, and folder links use the files currently available on this computer. '
  + 'A note is shared after a human reviews and approves that exact note. Automatic sharing is not active yet; permissions recorded now are for a future release. '
  + 'Linking a brain alone gives no ongoing or automatic permission to share notes.'

/** The exact audience, scope and permanence a person confirms before granting automatic sharing. */
export function automaticSharingConfirm(name: string, identity: string): string {
  return `Share notes to ${name} (${identity}) automatically? `
    + `Any note drafted for this brain can be published to everyone with access to ${identity} without another review. `
    + `Published notes stay in the repository's history even if they are removed later. `
    + 'Ciaobot does not copy your vault itself, but a note drafted for this brain can include private details from your memory or vault. Your memory pass and nightly care can choose private material for a draft too, not only a note you pick yourself. Other shared brains are not included. '
    + 'The repository address does not prove who can see it today: team membership and visibility can change outside Ciaobot. '
    + 'Automatic sharing is not active yet: this records a standing permission for when the producer ships, and the future publisher must verify the current target when it acts.'
}

/** Words for turning the standing permission back into review. */
export function revokeAutomaticSharingConfirm(name: string, identity: string): string {
  return `Stop sharing notes automatically to ${name} (${identity})? `
    + 'Notes already published stay in the repository\'s history. '
    + 'Future notes will wait for your review.'
}

export function sharedBrainPolicyText(state: SharedBrainState): string {
  if (state.sharing_grant_pending) {
    const base = 'Automatic sharing is paused, but its old grant still needs to be turned off.'
    return state.sharing_invalidation_failed
      ? `${base} The old grant could not be removed; turn it off to finish revoking it.`
      : base
  }
  if (state.sharing_policy === 'automatic') {
    const audience = `Automatic sharing is chosen for everyone who can access ${state.identity}; it is not active yet.`
    return state.problem ? `${audience} Paused until this folder is fixed.` : audience
  }
  return 'You review each note before it is shared.'
}

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
  inaccessible: { text: 'Folder is not readable', tone: 'error', detail: '' },
  registry_unavailable: {
    text: 'Inspection temporarily unavailable',
    tone: 'warn',
    detail: 'Try again in a moment.',
  },
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

/** Folder-specific words: availability on this computer, never a sync claim. */
const FOLDER_PROBLEM_TEXT: Record<string, SharedBrainStatus> = {
  missing: { text: 'Folder not available on this computer', tone: 'error', detail: '' },
  moved: { text: 'Folder location changed', tone: 'error', detail: '' },
  outside_install: { text: 'Folder is inside the Ciaobot install or a vault', tone: 'error', detail: '' },
  inaccessible: { text: 'Folder is not available to read on this computer', tone: 'error', detail: '' },
}

/** One plain-words status line per brain, so the state never rests on colour alone. */
export function sharedBrainStatus(state: SharedBrainState): SharedBrainStatus {
  // An ordinary folder is read in place: there is no pull, no upstream and no
  // update attempt, so "first update pending" would never resolve for it.
  if (state.kind === 'folder') {
    const folderProblem = FOLDER_PROBLEM_TEXT[state.problem]
      ?? (state.missing ? FOLDER_PROBLEM_TEXT.missing : undefined)
      ?? PROBLEM_TEXT[state.problem]
    if (folderProblem) return folderProblem
    return {
      text: 'Folder is locally available. Read in place, never updated.',
      tone: 'ok',
      detail: 'New notes are saved only after individual review; Ciaobot never replaces or deletes an existing folder file. This checks access on this computer only; it does not confirm that Google Drive, iCloud or another sync service has finished syncing.',
    }
  }
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
