import { describe, expect, it } from 'vitest'
import {
  DEFAULT_RESTART_MESSAGE,
  isRestartDrainMessage,
  MAX_RESTART_MESSAGE_CHARS,
  RESTART_DRAIN_MESSAGE,
  restartMessageForDisplay,
} from './serverRestart'

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

describe('restartMessageForDisplay', () => {
  it('passes our own messages through untouched', () => {
    expect(restartMessageForDisplay(RESTART_DRAIN_MESSAGE)).toBe(RESTART_DRAIN_MESSAGE)
  })

  it('falls back to the default for blank input', () => {
    expect(restartMessageForDisplay('')).toBe(DEFAULT_RESTART_MESSAGE)
    expect(restartMessageForDisplay('   \n ')).toBe(DEFAULT_RESTART_MESSAGE)
    expect(restartMessageForDisplay(undefined)).toBe(DEFAULT_RESTART_MESSAGE)
    expect(restartMessageForDisplay(null)).toBe(DEFAULT_RESTART_MESSAGE)
  })

  it('collapses whitespace so a multi-line error stays one paragraph', () => {
    expect(restartMessageForDisplay('  Restarting\n\n  now\t ')).toBe('Restarting now')
  })

  it('truncates an over-long forwarded error', () => {
    const long = `${'x'.repeat(500)} ${RESTART_DRAIN_MESSAGE}`
    const out = restartMessageForDisplay(long)
    expect(out.length).toBe(MAX_RESTART_MESSAGE_CHARS)
    expect(out.endsWith('…')).toBe(true)
  })

  it('keeps a message exactly at the limit whole', () => {
    const exact = 'y'.repeat(MAX_RESTART_MESSAGE_CHARS)
    expect(restartMessageForDisplay(exact)).toBe(exact)
  })
})
