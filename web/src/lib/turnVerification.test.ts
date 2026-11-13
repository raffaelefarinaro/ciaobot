import { describe, expect, it } from 'vitest'
import { applyTurnVerification, verificationBadge } from './turnVerification'
import type { ChatMessage, TurnVerification } from './types'

function verification(overrides: Partial<TurnVerification> = {}): TurnVerification {
  return { supported: 0, unsupported: 0, general: 0, claims: [], sources: [], ...overrides }
}

function row(role: ChatMessage['role'], turn_index?: number): ChatMessage {
  return { role, content: role, timestamp: '2026-11-12T10:00:00Z', ...(turn_index === undefined ? {} : { turn_index }) }
}

describe('verificationBadge', () => {
  it('returns null without vault claims', () => {
    expect(verificationBadge(verification({ general: 3 }))).toBeNull()
    expect(verificationBadge(undefined)).toBeNull()
    expect(verificationBadge(null)).toBeNull()
  })

  it('labels and tones a turn with an unsupported claim as a warning', () => {
    const badge = verificationBadge(verification({ supported: 4, unsupported: 1, general: 2 }))
    expect(badge?.label).toBe('Vault 4/5')
    expect(badge?.tone).toBe('warn')
    expect(badge?.ariaLabel).toContain('4 of 5')
    expect(badge?.ariaLabel).toContain('1 not supported')
  })

  it('labels a fully supported turn as ok', () => {
    const badge = verificationBadge(verification({ supported: 2 }))
    expect(badge?.label).toBe('Vault 2/2')
    expect(badge?.tone).toBe('ok')
    expect(badge?.ariaLabel).not.toContain('not supported')
  })
})

describe('applyTurnVerification', () => {
  it('marks the last assistant row of the turn', () => {
    const rows: ChatMessage[] = [
      row('user', 0),
      row('assistant'),
      row('user', 1),
      row('assistant'),
      row('system'),
      row('assistant'),
      row('user', 2),
      row('assistant'),
    ]
    const v = verification({ supported: 1 })
    expect(applyTurnVerification(rows, 1, v)).toBe(true)
    expect(rows.map(r => r.verification)).toEqual([undefined, undefined, undefined, undefined, undefined, v, undefined, undefined])
  })

  it('returns false for an unknown turn or a turn with no assistant row', () => {
    expect(applyTurnVerification([row('user', 0), row('assistant')], 9, verification())).toBe(false)
    expect(applyTurnVerification([row('user', 0)], 0, verification())).toBe(false)
  })
})
