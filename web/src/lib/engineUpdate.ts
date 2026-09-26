/**
 * Reading the engine update job the coordinator persists.
 *
 * One source of truth: the operation record behind `GET /api/update/status`
 * (`ciao/engine_update.py:PHASES`). The Settings card polls that route while a
 * run is in flight and derives everything it shows from the record's phase —
 * never from local guesses about how far a run got. Every phase in `PHASES`
 * appears in exactly one group below, so a phase added in Python shows up here
 * as a test failure rather than as "unknown" in the card.
 */
import type { EngineUpdateOperation } from './types'

/** Phases grouped by what the card does with them, not by where they run. */
export const UPDATE_PHASES = {
  /** The staging half: runs while the engine is up. */
  staging: ['resolving', 'downloading', 'verifying', 'staging'],
  /** The apply half, plus the rollback that is part of the same transaction.
   *  `rolling_back` is a live phase, not a failure: the engine may still go
   *  down before it lands back on the previous version, so the card keeps
   *  polling it. */
  applying: [
    'draining',
    'applying',
    'stopping',
    'swapping',
    'starting',
    'verifying_start',
    'rolling_back',
  ],
  /** Nothing further happens without a new run. */
  terminal: ['applied', 'failed', 'rolled_back', 'rollback_failed'],
} as const

/** Terminal phases that ended the run without applying the update. */
export const UPDATE_FAILURE_PHASES = ['failed', 'rolled_back', 'rollback_failed'] as const

/** The card states a persisted record maps onto. */
export type UpdateStage =
  | 'idle'
  | 'staging'
  | 'staged'
  | 'applying'
  | 'done'
  | 'failed'
  | 'rolled_back'

/** The `staged` phase sits between the two halves: staged, not yet in flight. */
export const UPDATE_STAGED_PHASE = 'staged'

/** The terminal phase that means the new version is running. */
export const UPDATE_APPLIED_PHASE = 'applied'

function phaseOf(op: EngineUpdateOperation | null): string {
  return op?.phase || ''
}

function inList(phases: readonly string[], phase: string): boolean {
  return phases.includes(phase)
}

/** The card state a persisted record describes. */
export function updateStage(op: EngineUpdateOperation | null): UpdateStage {
  const phase = phaseOf(op)
  if (!phase) return 'idle'
  if (inList(UPDATE_PHASES.staging, phase)) return 'staging'
  if (phase === UPDATE_STAGED_PHASE) return 'staged'
  if (inList(UPDATE_PHASES.applying, phase)) return 'applying'
  if (phase === UPDATE_APPLIED_PHASE) return 'done'
  if (phase === 'failed') return 'failed'
  if (phase === 'rolled_back' || phase === 'rollback_failed') return 'rolled_back'
  return 'idle'
}

/** True for a card state that still describes a run in flight. */
export function updateStageInFlight(stage: UpdateStage): boolean {
  return stage === 'staging' || stage === 'applying'
}

/** True while the record still describes a run in flight. */
export function updateInFlight(op: EngineUpdateOperation | null): boolean {
  return updateStageInFlight(updateStage(op))
}

/** True for a phase that ended the run without applying the update. */
export function isUpdateFailurePhase(phase: string): boolean {
  return inList(UPDATE_FAILURE_PHASES, phase)
}

/** True for a record that ended the run without applying the update. */
export function updateFailed(op: EngineUpdateOperation | null): boolean {
  return isUpdateFailurePhase(phaseOf(op))
}

const PHASE_LABELS: Record<string, string> = {
  resolving: 'Looking for the latest release',
  downloading: 'Downloading the release',
  verifying: 'Checking the signed release',
  staging: 'Preparing the update',
  staged: 'Ready to apply',
  draining: 'Waiting for active chats to finish',
  applying: 'Applying the update',
  stopping: 'Stopping the engine',
  swapping: 'Swapping in the new version',
  starting: 'Starting the updated engine',
  verifying_start: 'Checking the updated engine is answering',
  applied: 'Update applied',
  rolling_back: 'Rolling back to the previous version',
  rolled_back: 'Rolled back to the previous version',
  rollback_failed: 'The rollback did not finish',
  failed: 'The update failed',
}

/**
 * Plain-language name for one phase.
 *
 * The phase string is an implementation detail, so it never reaches the card.
 * Empty for a phase this build has no name for, which keeps an unknown phase
 * from rendering as a bare identifier.
 */
export function updatePhaseName(phase: string): string {
  return PHASE_LABELS[phase] || ''
}

/**
 * The record's phase as a sentence, with the version named where the record
 * says which version is in play.
 */
export function updatePhaseLabel(op: EngineUpdateOperation | null): string {
  const phase = phaseOf(op)
  if (!phase) return ''
  const name = updatePhaseName(phase)
  if (name && (phase === 'rolling_back' || phase === 'rolled_back') && op?.from_version) {
    return `${name} (v${op.from_version})`
  }
  return name
}

const FAILURE_SENTENCES: Record<string, string> = {
  failed: 'The update failed before it could be applied.',
  rolled_back: 'The update failed and the previous version was restored.',
  rollback_failed: 'The update failed and the previous version could not be restored.',
}

/**
 * What to say about a run that failed, for a phase that names no reason.
 * Empty for any phase that is not a failure.
 */
export function updateFailureSentence(phase: string): string {
  return FAILURE_SENTENCES[phase] || ''
}

/**
 * Why a run failed: the record's own `error` when it wrote one, else a sentence
 * about the phase. Never empty for a failure phase — a failure with no text
 * would read as a card with nothing to say.
 */
export function updateFailureText(op: EngineUpdateOperation | null): string {
  const recorded = (op?.error || '').trim()
  if (recorded) return recorded
  return updateFailureSentence(phaseOf(op))
}
