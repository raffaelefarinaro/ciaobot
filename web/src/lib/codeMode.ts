// Code mode in the transcript. The engine keeps switch markers on the chat
// (`code_mode_events`), not in the message rows, so the client places them.
//
// Anchor contract: `after_turn_index` is the index of the last turn before the
// switch (-1 means before any turn). It is matched against the server-assigned
// `turn_index` on user rows. A marker goes right before the first user row whose
// `turn_index` is greater than its anchor, or at the end when there is none.
import type { ChatMessage, CodeModeActor, CodeModeEvent, ProjectRepo } from './types'

export const CODE_MODE_MARKER_TOOL = '_code_mode'

export function codeModeMarkerText(on: boolean, actor: CodeModeActor): string {
  return `${on ? 'Switched to code mode' : 'Left code mode'} · by ${actor === 'agent' ? 'Ciao' : 'you'}`
}

export function isCodeModeMarker(m: Pick<ChatMessage, 'role' | 'tool_name'>): boolean {
  return m.role === 'system' && m.tool_name === CODE_MODE_MARKER_TOOL
}

/** Mirrors the engine rule `chat.code_mode and project.repo_path`. */
export function isChatCodeModeOn(
  chat: { code_mode?: boolean } | null | undefined,
  repo: ProjectRepo | null | undefined,
): boolean {
  return Boolean(chat?.code_mode) && Boolean(repo)
}

export function codeModeMarkerRow(ev: CodeModeEvent): ChatMessage {
  return {
    role: 'system',
    tool_name: CODE_MODE_MARKER_TOOL,
    content: codeModeMarkerText(ev.code_mode, ev.actor),
    timestamp: ev.at,
  }
}

function hasTurn(m: ChatMessage): m is ChatMessage & { turn_index: number } {
  return m.role === 'user' && typeof m.turn_index === 'number'
}

export function interleaveCodeModeEvents(
  messages: ChatMessage[],
  events: CodeModeEvent[] | undefined,
  opts: { hasOlder: boolean },
): ChatMessage[] {
  if (!events?.length) return messages
  let pending = events
    .filter((e) => typeof e.after_turn_index === 'number')
    .sort((a, b) => a.after_turn_index - b.after_turn_index || (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
  const first = messages.find(hasTurn)
  // Older pages are not loaded: events above the loaded window are not ours to show.
  if (opts.hasOlder && first) {
    const floor = first.turn_index - 1
    pending = pending.filter((e) => e.after_turn_index >= floor)
  }
  const out: ChatMessage[] = []
  let next = 0
  for (const m of messages) {
    if (hasTurn(m)) {
      while (next < pending.length && pending[next].after_turn_index < m.turn_index) {
        out.push(codeModeMarkerRow(pending[next++]))
      }
    }
    out.push(m)
  }
  while (next < pending.length) out.push(codeModeMarkerRow(pending[next++]))
  return out
}
