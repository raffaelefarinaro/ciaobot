import { describe, expect, it } from 'vitest'
import { backgroundRunElapsed, backgroundRunLabel, backgroundRunOutcome } from './backgroundRuns'
import type { BackgroundRunSummary } from './types'

function run(over: Partial<BackgroundRunSummary> = {}): BackgroundRunSummary {
  return { run_id: 'r1', label: '', cmd: ['uv', 'sync'], started_at: '2026-10-06T10:00:00Z', status: 'running', exit_code: null, ...over }
}

describe('backgroundRuns', () => {
  it('prefers the label and falls back to the command', () => {
    expect(backgroundRunLabel(run({ label: 'Clone rizzo-flow' }))).toBe('Clone rizzo-flow')
    expect(backgroundRunLabel(run())).toBe('uv sync')
  })

  it('counts whole seconds, then minutes', () => {
    const start = Date.parse('2026-10-06T10:00:00Z')
    expect(backgroundRunElapsed(run(), start + 4_300)).toBe('4s')
    expect(backgroundRunElapsed(run(), start + 125_400)).toBe('2m 5s')
    expect(backgroundRunElapsed(run({ started_at: '' }), start)).toBe('')
  })

  it('says how a run ended', () => {
    expect(backgroundRunOutcome(run({ status: 'ok' }))).toEqual({ text: 'uv sync finished', tone: 'ok' })
    expect(backgroundRunOutcome(run({ status: 'error', exit_code: 1 }))).toEqual({ text: 'uv sync failed (exit 1)', tone: 'error' })
    expect(backgroundRunOutcome(run({ status: 'cancelled' })).tone).toBe('muted')
  })
})
