/**
 * The keys a soft keyboard cannot produce, and the bytes a hardware key sends.
 *
 * The arrows follow the terminal's cursor-key mode (DECCKM): in normal mode
 * xterm sends CSI (`ESC [ A`); once a program switched to application mode
 * (`CSI ? 1 h`) it sends SS3 (`ESC O A`). The on-screen arrows do the same.
 * Enter is a carriage return, as xterm's own Enter is.
 */
export type TerminalKey =
  | 'escape' | 'tab' | 'ctrl-c' | 'ctrl-d' | 'up' | 'down' | 'left' | 'right' | 'enter'

export interface TerminalKeyDef {
  readonly key: TerminalKey
  /** What the button shows. */
  readonly label: string
  /** The accessible name (aria-label and title). */
  readonly name: string
}

/** The row, in display order. */
export const TERMINAL_KEYS: ReadonlyArray<TerminalKeyDef> = [
  { key: 'escape', label: 'Esc', name: 'Escape' },
  { key: 'tab', label: 'Tab', name: 'Tab' },
  { key: 'ctrl-c', label: 'Ctrl+C', name: 'Control C' },
  { key: 'ctrl-d', label: 'Ctrl+D', name: 'Control D' },
  { key: 'up', label: '↑', name: 'Up arrow' },
  { key: 'down', label: '↓', name: 'Down arrow' },
  { key: 'left', label: '←', name: 'Left arrow' },
  { key: 'right', label: '→', name: 'Right arrow' },
  { key: 'enter', label: 'Enter', name: 'Enter' },
]

const ARROW_FINAL: Partial<Record<TerminalKey, string>> = { up: 'A', down: 'B', right: 'C', left: 'D' }
const PLAIN: Partial<Record<TerminalKey, string>> = {
  escape: '\x1b', tab: '\t', 'ctrl-c': '\x03', 'ctrl-d': '\x04', enter: '\r',
}

/** The byte sequence a hardware key sends, as a 7-bit string. */
export function terminalKeyBytes(key: TerminalKey, applicationCursor: boolean): string {
  const final = ARROW_FINAL[key]
  if (final) return (applicationCursor ? '\x1bO' : '\x1b[') + final
  const plain = PLAIN[key]
  if (plain === undefined) throw new Error(`Unknown terminal key: ${key}`)
  return plain
}
