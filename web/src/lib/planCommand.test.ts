// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearPlanReturnMode,
  rememberPlanReturnMode,
  restorePlanReturnMode,
} from './planCommand'

describe('plan return mode', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('round-trips a remembered mode for the chat that stored it', () => {
    rememberPlanReturnMode('chat-1', 'bypass')
    expect(restorePlanReturnMode('chat-1')).toBe('bypass')
    expect(restorePlanReturnMode('chat-2')).toBe('auto')
  })

  it('falls back to auto for an unknown or unstorable mode', () => {
    rememberPlanReturnMode('chat-1', 'plan')
    expect(restorePlanReturnMode('chat-1')).toBe('auto')
    rememberPlanReturnMode('chat-1', 'nonsense')
    expect(restorePlanReturnMode('chat-1')).toBe('auto')
  })

  it('drops the stored mode once it is cleared', () => {
    rememberPlanReturnMode('chat-1', 'normal')
    clearPlanReturnMode('chat-1')
    expect(restorePlanReturnMode('chat-1')).toBe('auto')
  })
})
