import { describe, expect, it } from 'vitest'
import { readyToAdvance, type StageState } from '../onboarding'

const ready = (over: Partial<StageState> = {}): StageState => ({ dirty: false, pending: false, canContinue: true, error: null, ...over })

describe('readyToAdvance', () => {
  it('follows the stage for an ordinary step', () => {
    expect(readyToAdvance('runtime', ready())).toBe(true)
    expect(readyToAdvance('runtime', ready({ canContinue: false }))).toBe(false)
  })

  it('follows the access stage on the access screen, not the shell', () => {
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready())).toBe(true)
    expect(readyToAdvance('access_memory', ready(), ready({ canContinue: false }))).toBe(false)
    expect(readyToAdvance('access_memory', ready(), null)).toBe(false)
  })

  it('refuses while the access stage is busy or failed', () => {
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready({ pending: true }))).toBe(false)
    expect(readyToAdvance('access_memory', ready({ canContinue: false }), ready({ error: 'offline' }))).toBe(false)
  })
})
