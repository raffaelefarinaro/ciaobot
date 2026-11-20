import { describe, expect, it } from 'vitest'
import {
  newShellCard,
  rememberShellCall,
  settleShellCard,
  shellCardFromHistory,
  shellTitle,
} from './shellCard'

describe('shell card', () => {
  it('starts running with no exit code and no output', () => {
    const card = newShellCard('c1', '', false)
    expect(card).toEqual({ tool_use_id: 'c1', command: '', background: false, state: 'running', exit_code: null, output_tail: '' })
    expect(shellTitle(card)).toBe('Shell')
  })

  it('takes a later command and only turns the background flag on', () => {
    const card = newShellCard('c1', 'ls', false)
    rememberShellCall(card, '', true)
    expect(card.command).toBe('ls')
    expect(card.background).toBe(true)
    rememberShellCall(card, 'ls -la', false)
    expect(card.command).toBe('ls -la')
    expect(card.background).toBe(true)
  })

  it('settles a foreground call on its result: exit zero is done', () => {
    const card = newShellCard('c1', 'echo hi', false)
    settleShellCard(card, { exit_code: 0, output_tail: 'hi' })
    expect(card).toMatchObject({ state: 'done', exit_code: 0, output_tail: 'hi' })
  })

  it('fails on a non-zero exit code and on an error result', () => {
    const exited = newShellCard('c1', 'false', false)
    settleShellCard(exited, { exit_code: 2, output_tail: 'boom' })
    expect(exited).toMatchObject({ state: 'failed', exit_code: 2 })

    const errored = newShellCard('c2', 'make', false)
    settleShellCard(errored, { is_error: true })
    expect(errored).toMatchObject({ state: 'failed', exit_code: null })
  })

  it('keeps a background launch acknowledgement running, never done', () => {
    const card = newShellCard('c1', 'sleep 9', false)
    settleShellCard(card, { background: true, is_error: false, exit_code: null, output_tail: '' })
    expect(card).toMatchObject({ state: 'running', background: true, exit_code: null })
  })

  it('settles a background shell on a later result for the call', () => {
    const card = newShellCard('c1', 'sleep 9', true)
    settleShellCard(card, { background: true })
    settleShellCard(card, { exit_code: 0, output_tail: 'done' })
    expect(card).toMatchObject({ state: 'done', background: true, exit_code: 0, output_tail: 'done' })
  })

  it('reads a history row tolerantly, and drops a row with no id', () => {
    expect(shellCardFromHistory({ tool_use_id: 'h1', command: 'ls', state: 'failed', exit_code: 1, output_tail: 'x', background: true }))
      .toEqual({ tool_use_id: 'h1', command: 'ls', background: true, state: 'failed', exit_code: 1, output_tail: 'x' })
    expect(shellCardFromHistory({ tool_use_id: 'h2', state: 'weird', exit_code: 'x' }))
      .toEqual({ tool_use_id: 'h2', command: '', background: false, state: 'running', exit_code: null, output_tail: '' })
    expect(shellCardFromHistory({ command: 'no id' })).toBeNull()
    expect(shellCardFromHistory(null)).toBeNull()
  })
})
