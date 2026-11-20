/* ------------------------------------------------------------------ *
 * One shell call as one card: the shape the live trace and the history
 * renderer both produce (`ciao/web/transcript_service.py`, `_ShellCard`,
 * mirrors these keys). A card is keyed by its call's tool_use_id.
 *
 * Its state word and settle rules are the subagent card's (`subagentCard.ts`):
 * the same `SubagentState`. A background shell's launch acknowledgement leaves
 * it running; nothing on the wire says when it ends, so it stays running until
 * a later result for the call arrives. Pure helpers, no Vue imports.
 * ------------------------------------------------------------------ */

import type { SubagentState } from './subagentCard'

export type ShellState = SubagentState

export interface ShellCard {
  tool_use_id: string
  /** The command, one line of plain text (the server bounds it). */
  command: string
  /** Started in the background: its badge says so while it runs. */
  background: boolean
  state: ShellState
  /** The foreground exit code, null when the agent sends none or the shell is still running. */
  exit_code: number | null
  /** The last lines of the output, plain text, bounded by the server. */
  output_tail: string
}

/** The fields of a shell `tool_result` the card settles from. */
export interface ShellResult {
  background?: boolean
  is_error?: boolean
  exit_code?: number | null
  output_tail?: string
}

export function newShellCard(toolUseId: string, command: string, background: boolean): ShellCard {
  return {
    tool_use_id: toolUseId,
    command,
    background,
    state: 'running',
    exit_code: null,
    output_tail: '',
  }
}

/** A re-announced call with a command fills in the card's; the background flag only turns on. */
export function rememberShellCall(card: ShellCard, command: string, background: boolean): void {
  if (command) card.command = command
  card.background = card.background || background
}

/** Apply a `tool_result` for the call. A launch acknowledgement keeps it running. Otherwise a
 *  non-zero exit code or an error fails it, and anything else is done. */
export function settleShellCard(card: ShellCard, result: ShellResult): void {
  if (result.background) {
    card.background = true
    card.state = 'running'
    return
  }
  const code = typeof result.exit_code === 'number' ? result.exit_code : null
  card.exit_code = code
  card.state = result.is_error || (code !== null && code !== 0) ? 'failed' : 'done'
  card.output_tail = typeof result.output_tail === 'string' ? result.output_tail : ''
}

/** The header text: the command, or "Shell" when the call has none yet. */
export function shellTitle(card: Pick<ShellCard, 'command'>): string {
  return card.command || 'Shell'
}

/** Wire-shaped card from a history row, tolerant of unknown or missing fields. A row
 *  without an id is not a card; an unknown state reads as running. */
export function shellCardFromHistory(raw: unknown): ShellCard | null {
  if (!raw || typeof raw !== 'object') return null
  const data = raw as Record<string, unknown>
  if (typeof data.tool_use_id !== 'string') return null
  const state: ShellState = data.state === 'done' || data.state === 'failed' ? data.state : 'running'
  return {
    tool_use_id: data.tool_use_id,
    command: typeof data.command === 'string' ? data.command : '',
    background: data.background === true,
    state,
    exit_code: typeof data.exit_code === 'number' ? data.exit_code : null,
    output_tail: typeof data.output_tail === 'string' ? data.output_tail : '',
  }
}
