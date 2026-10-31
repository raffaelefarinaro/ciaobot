import { describe, expect, it } from 'vitest'
import { TERMINAL_KEYS, terminalKeyBytes, type TerminalKey } from './terminalKeys'
import { TERMINAL_INPUT_MAX_BYTES } from './terminalProtocol'

describe('TERMINAL_KEYS', () => {
  it('lists the nine keys in row order with their labels and accessible names', () => {
    expect(TERMINAL_KEYS.map(k => k.key)).toEqual([
      'escape', 'tab', 'ctrl-c', 'ctrl-d', 'up', 'down', 'left', 'right', 'enter',
    ])
    expect(TERMINAL_KEYS.map(k => k.name)).toEqual([
      'Escape', 'Tab', 'Control C', 'Control D', 'Up arrow', 'Down arrow', 'Left arrow', 'Right arrow', 'Enter',
    ])
    expect(TERMINAL_KEYS.map(k => k.label)).toEqual([
      'Esc', 'Tab', 'Ctrl+C', 'Ctrl+D', '↑', '↓', '←', '→', 'Enter',
    ])
  })
})

describe('terminalKeyBytes', () => {
  it('sends the control bytes a hardware key sends, in either cursor mode', () => {
    for (const applicationCursor of [false, true]) {
      expect(terminalKeyBytes('escape', applicationCursor)).toBe('\x1b')
      expect(terminalKeyBytes('tab', applicationCursor)).toBe('\t')
      expect(terminalKeyBytes('ctrl-c', applicationCursor)).toBe('\x03')
      expect(terminalKeyBytes('ctrl-d', applicationCursor)).toBe('\x04')
      expect(terminalKeyBytes('enter', applicationCursor)).toBe('\r')
    }
  })

  it('sends CSI arrows in normal cursor mode', () => {
    expect(terminalKeyBytes('up', false)).toBe('\x1b[A')
    expect(terminalKeyBytes('down', false)).toBe('\x1b[B')
    expect(terminalKeyBytes('right', false)).toBe('\x1b[C')
    expect(terminalKeyBytes('left', false)).toBe('\x1b[D')
  })

  it('sends SS3 arrows in application cursor mode', () => {
    expect(terminalKeyBytes('up', true)).toBe('\x1bOA')
    expect(terminalKeyBytes('down', true)).toBe('\x1bOB')
    expect(terminalKeyBytes('right', true)).toBe('\x1bOC')
    expect(terminalKeyBytes('left', true)).toBe('\x1bOD')
  })

  it('keeps every sequence 7-bit and within one input frame', () => {
    for (const { key } of TERMINAL_KEYS) {
      for (const applicationCursor of [false, true]) {
        const bytes: string = terminalKeyBytes(key as TerminalKey, applicationCursor)
        for (let i = 0; i < bytes.length; i += 1) {
          expect(bytes.charCodeAt(i)).toBeLessThan(0x80)
        }
        expect(bytes.length).toBeLessThanOrEqual(TERMINAL_INPUT_MAX_BYTES)
      }
    }
  })
})
