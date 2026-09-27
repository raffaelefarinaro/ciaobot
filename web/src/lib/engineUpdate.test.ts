import { describe, expect, it } from 'vitest'
import {
  UPDATE_APPLIED_PHASE,
  UPDATE_PHASES,
  UPDATE_STAGED_PHASE,
  isUpdateFailurePhase,
  updateFailed,
  updateFailureSentence,
  updateFailureText,
  updateInFlight,
  updatePhaseLabel,
  updatePhaseName,
  updateStage,
  updateStageInFlight,
} from './engineUpdate'
import type { EngineUpdateOperation } from './types'

/**
 * Every phase `ciao/engine_update.py:PHASES` names, listed literally. The list
 * is the point: a phase added on the Python side has to be added here, where a
 * missed one fails a test, rather than reaching the card as "unknown".
 */
const PHASES = [
  'resolving',
  'downloading',
  'verifying',
  'staging',
  'staged',
  'draining',
  'applying',
  'stopping',
  'swapping',
  'starting',
  'verifying_start',
  'applied',
  'rolling_back',
  'rolled_back',
  'rollback_failed',
  'failed',
] as const

const FAILURE_PHASES = ['failed', 'rolled_back', 'rollback_failed'] as const

function op(phase: string, extra: Partial<EngineUpdateOperation> = {}): EngineUpdateOperation {
  return {
    id: '20260926T101500Z-ab12',
    phase,
    from_version: '0.19.0',
    to_version: '0.20.0',
    started_at: '2026-09-26T10:15:00+00:00',
    updated_at: '2026-09-26T10:15:20+00:00',
    ...extra,
  }
}

describe('the phase groups cover the coordinator', () => {
  it('places every PHASES member in exactly one group, and invents none', () => {
    const grouped = [
      ...UPDATE_PHASES.staging,
      UPDATE_STAGED_PHASE,
      ...UPDATE_PHASES.applying,
      ...UPDATE_PHASES.terminal,
    ]
    expect([...grouped].sort()).toEqual([...PHASES].sort())
    expect(new Set(grouped).size).toBe(grouped.length)
  })

  it('keeps the single-phase constants in step with the groups', () => {
    expect(UPDATE_PHASES.terminal).toContain(UPDATE_APPLIED_PHASE)
    expect(UPDATE_PHASES.terminal).not.toContain(UPDATE_STAGED_PHASE)
  })
})

describe('updateStage', () => {
  it('reads no record as idle, and each phase as its own state', () => {
    expect(updateStage(null)).toBe('idle')
    const expected: Record<string, string> = {
      resolving: 'staging',
      downloading: 'staging',
      verifying: 'staging',
      staging: 'staging',
      staged: 'staged',
      draining: 'applying',
      applying: 'applying',
      stopping: 'applying',
      swapping: 'applying',
      starting: 'applying',
      verifying_start: 'applying',
      applied: 'done',
      rolling_back: 'applying',
      rolled_back: 'rolled_back',
      rollback_failed: 'rolled_back',
      failed: 'failed',
    }
    for (const phase of PHASES) {
      expect([phase, updateStage(op(phase))]).toEqual([phase, expected[phase]])
    }
    // A phase no build knows is not claimed as progress.
    expect(updateStage(op('not_a_phase'))).toBe('idle')
  })
})

describe('updateInFlight', () => {
  it('is true only while a run can still move on its own', () => {
    expect(updateInFlight(op('downloading'))).toBe(true)
    expect(updateInFlight(op('swapping'))).toBe(true)
    // A rollback is still a run: the engine may restart before it lands.
    expect(updateInFlight(op('rolling_back'))).toBe(true)
    // Staged is the pause between the two halves, not a run.
    expect(updateInFlight(op('staged'))).toBe(false)
    expect(updateInFlight(op('applied'))).toBe(false)
    expect(updateInFlight(op('rolled_back'))).toBe(false)
    expect(updateInFlight(null)).toBe(false)
  })

  it('answers the same from a card state, so a settled record is recognisable', () => {
    // The card watches the state rather than the record, and must clear its
    // in-flight line on exactly the transitions the record says settled.
    for (const phase of PHASES) {
      expect(updateStageInFlight(updateStage(op(phase)))).toBe(updateInFlight(op(phase)))
    }
    expect(updateStageInFlight('idle')).toBe(false)
  })
})

describe('updateFailed', () => {
  it('is true for the terminal phases that did not apply the update', () => {
    for (const phase of FAILURE_PHASES) {
      expect(updateFailed(op(phase))).toBe(true)
      expect(isUpdateFailurePhase(phase)).toBe(true)
    }
    expect(updateFailed(op('applied'))).toBe(false)
    expect(updateFailed(op('staged'))).toBe(false)
    expect(updateFailed(null)).toBe(false)
  })
})

describe('updateFailureText', () => {
  it('prefers the reason the record wrote', () => {
    expect(updateFailureText(op('failed', { error: 'signature check failed' })))
      .toBe('signature check failed')
  })

  it('still says something for a failure that recorded no reason', () => {
    for (const phase of FAILURE_PHASES) {
      expect(updateFailureText(op(phase)).length).toBeGreaterThan(0)
      expect(updateFailureSentence(phase).length).toBeGreaterThan(0)
    }
  })

  it('invents no failure for a phase that is not one', () => {
    for (const phase of PHASES) {
      if ((FAILURE_PHASES as readonly string[]).includes(phase)) continue
      expect(updateFailureText(op(phase))).toBe('')
    }
    expect(updateFailureText(null)).toBe('')
  })
})

describe('updatePhaseName / updatePhaseLabel', () => {
  it('names every phase in plain language, and never the identifier', () => {
    for (const phase of PHASES) {
      const name = updatePhaseName(phase)
      expect(name.length).toBeGreaterThan(0)
      expect(name).not.toBe(phase)
    }
    expect(updatePhaseName('not_a_phase')).toBe('')
    expect(updatePhaseName('')).toBe('')
  })

  it('names the version a rollback is heading back to', () => {
    expect(updatePhaseLabel(op('rolled_back'))).toContain('v0.19.0')
    expect(updatePhaseLabel(op('rolling_back'))).toContain('v0.19.0')
    // A forward phase is about the version being installed, not the one running.
    expect(updatePhaseLabel(op('staging'))).not.toContain('v0.19.0')
  })

  it('has nothing to say without a record', () => {
    expect(updatePhaseLabel(null)).toBe('')
  })
})
