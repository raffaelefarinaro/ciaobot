import { describe, expect, it } from 'vitest'
import {
  CARE_CYCLE_LEASE_SENTENCE,
  blockerSentences,
  careCycleView,
  readySummary,
  undoSentence,
} from './careCycle'
import type { CareCycleHostResponse } from './types'

function host(overrides: Partial<CareCycleHostResponse> = {}): CareCycleHostResponse {
  return {
    workspace: 'personal',
    on_host: true,
    lease_live: false,
    status: 'v1',
    live_lease: null,
    marker: 'present',
    last_full_pass: '2026-11-09',
    daily: 0,
    weekly_historical: 0,
    ambiguous: [],
    dropped: [],
    backup_path: '/vault/Workspace/Curation-State.v1.bak',
    digest: 'abc',
    blockers: [],
    ...overrides,
  }
}

describe('careCycleView', () => {
  it('a browser that is not on the host gets the remote state and no report', () => {
    expect(careCycleView({ workspace: 'personal', on_host: false, reason: 'not_on_host', lease_live: null })).toEqual({
      kind: 'not-on-host',
    })
  })

  it('a v2 vault is switched, read-only', () => {
    expect(careCycleView(host({ status: 'v2' })).kind).toBe('switched')
  })

  it('a clean v1 preview is ready to switch', () => {
    expect(careCycleView(host()).kind).toBe('ready')
  })

  it('a live lease blocks the switch with the one run-in-progress sentence', () => {
    const view = careCycleView(
      host({
        lease_live: true,
        live_lease: { holder: 'run', started_at: 'x', expires_at: 'y' },
        blockers: ['a live curation lease holds this vault; wait for the run to finish or expire'],
      }),
    )
    expect(view.kind).toBe('blocked')
    if (view.kind !== 'blocked') throw new Error('unreachable')
    expect(view.running).toBe(true)
    expect(view.reasons).toEqual([CARE_CYCLE_LEASE_SENTENCE])
  })

  it('an unreadable state is blocked and says so', () => {
    const view = careCycleView(host({ status: 'unreadable', blockers: ['the v1 state cannot be read safely: bad'] }))
    expect(view.kind).toBe('blocked')
    if (view.kind !== 'blocked') throw new Error('unreachable')
    expect(view.unreadable).toBe(true)
    expect(view.running).toBe(false)
    expect(view.reasons).toEqual(['the v1 state cannot be read safely: bad'])
  })
})

describe('blockerSentences', () => {
  it('keeps every other blocker, once, in order', () => {
    const sentences = blockerSentences(
      host({ blockers: ['first', 'first', 'second'], live_lease: null }),
    )
    expect(sentences).toEqual(['first', 'second'])
  })

  it('drops the raw lease text when the lease sentence stands in for it', () => {
    const sentences = blockerSentences(
      host({
        live_lease: { holder: 'run' },
        blockers: ['a live curation lease holds this vault; wait for the run to finish or expire'],
      }),
    )
    expect(sentences).toEqual([CARE_CYCLE_LEASE_SENTENCE])
  })
})

describe('readySummary', () => {
  it('counts what a switch keeps, in plain words', () => {
    expect(
      readySummary({
        ...host(),
        daily: 1,
        weekly_historical: 3,
        ambiguous: ['a'],
        dropped: [],
      }),
    ).toEqual([
      '1 daily progress record kept.',
      '3 weekly records kept as history. They need fresh proof before they count again.',
      '1 weekly record is ambiguous and needs fresh proof.',
    ])
  })

  it('reports dropped daily records only when there are some', () => {
    const lines = readySummary({ ...host(), dropped: ['x', 'y'] })
    expect(lines[lines.length - 1]).toBe('2 daily records dated after today are dropped.')
  })
})

describe('undoSentence', () => {
  it('states the manual way back and never claims an automatic undo', () => {
    const text = undoSentence()
    expect(text).toContain('Workspace/Curation-State.v1.bak')
    expect(text).toContain('Ciaobot does not undo this automatically')
    expect(text).toContain('progress made after the switch is not carried back')
  })
})
