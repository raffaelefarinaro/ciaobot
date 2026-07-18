import { describe, expect, it } from 'vitest'
import { isRestartDrainMessage, RESTART_DRAIN_MESSAGE } from './serverRestart'

describe('isRestartDrainMessage', () => {
  it('matches the canonical drain rejection', () => {
    expect(isRestartDrainMessage(RESTART_DRAIN_MESSAGE)).toBe(true)
  })

  it('matches Error-prefixed chat copy', () => {
    expect(isRestartDrainMessage(`Error: ${RESTART_DRAIN_MESSAGE}`)).toBe(true)
  })

  it('rejects unrelated errors', () => {
    expect(isRestartDrainMessage('chat not found')).toBe(false)
    expect(isRestartDrainMessage('')).toBe(false)
    expect(isRestartDrainMessage(null)).toBe(false)
  })
})
