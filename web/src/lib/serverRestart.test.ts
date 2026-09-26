import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_RESTART_MESSAGE,
  isRestartDrainMessage,
  MAX_RESTART_MESSAGE_CHARS,
  reloadWhenServerReady,
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

describe('reloadWhenServerReady', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  /** A startup-status that is down, then ready — the restart we wait for. */
  function stubEngine(down: number) {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      if (calls <= down) return { ok: false }
      return { ok: true, json: async () => ({ overall_ready: true }) }
    }))
  }

  it('reloads once the server has been down and is back', async () => {
    const reload = vi.fn()
    vi.stubGlobal('location', { reload })
    stubEngine(1)

    await reloadWhenServerReady(60_000)

    expect(reload).toHaveBeenCalledTimes(1)
  })

  // An update drain that gives up reopens admission on a healthy engine. A
  // loop left running would hard-reload every tab at the end of its timeout,
  // throwing away unsent drafts for a restart that is never coming.
  it('never reloads once the signal is aborted', async () => {
    const reload = vi.fn()
    vi.stubGlobal('location', { reload })
    stubEngine(1)
    const controller = new AbortController()

    const reloading = reloadWhenServerReady(60_000, controller.signal)
    controller.abort()
    await reloading

    expect(reload).not.toHaveBeenCalled()
  })

  // The abort can land during the poll that observes the engine back, which is
  // the reading that would otherwise reload.
  it('does not reload on the poll that observes the server back', async () => {
    const reload = vi.fn()
    vi.stubGlobal('location', { reload })
    const controller = new AbortController()
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      // The engine goes down, then the drain is cancelled and the engine
      // comes back on the very next reading.
      if (calls === 1) return { ok: false }
      controller.abort()
      return { ok: true, json: async () => ({ overall_ready: true }) }
    }))

    await reloadWhenServerReady(60_000, controller.signal)

    expect(reload).not.toHaveBeenCalled()
  })
})
