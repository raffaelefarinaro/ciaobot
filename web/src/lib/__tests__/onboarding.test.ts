import { describe, expect, it } from 'vitest'
import { readyToAdvance, type StageState } from '../onboarding'

const ready = (over: Partial<StageState> = {}): StageState => ({ dirty: false, pending: false, canContinue: true, error: null, ...over })

describe('readyToAdvance', () => {
  it('follows the stage for an ordinary step', () => {
    expect(readyToAdvance('runtime', ready())).toBe(true)
    expect(readyToAdvance('runtime', ready({ canContinue: false }))).toBe(false)
  })

  it('needs both halves of the access/memory screen', () => {
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready(), ready())).toBe(true)
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready(), ready({ canContinue: false }))).toBe(false)
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready(), null)).toBe(false)
  })

  it('refuses while either half is busy or failed', () => {
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready({ pending: true }), ready())).toBe(false)
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready(), ready({ error: 'offline' }))).toBe(false)
  })
})
