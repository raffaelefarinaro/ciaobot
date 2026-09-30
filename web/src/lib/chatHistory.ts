// Pure transforms over chat history rows: normalising what the renderer may
// show, mapping a server row into a `ChatMessage`, and reconciling a freshly
// loaded server history against the client's live tail.
//
// No state and no Vue imports. `stores/projects.ts` owns `messages` and
// decides *when* to reconcile; this module only computes the result.

import { isPlausibleFilePath } from './filePaths'
import { isHostConnectionUnavailableMessage } from './chatWs'
import type { ChatMessage } from './types'

export function stripLegacyContextPrefix(content: string): string {
  const lines = content.split('\n')
  let idx = 0
  let seenContext = false

  while (idx < lines.length) {
    const line = lines[idx]
    if (!line.trim()) {
      if (seenContext) {
        const remainder = lines.slice(idx + 1).join('\n').trim()
        return remainder || content
      }
      idx += 1
      continue
    }
    if (
      line.startsWith('[CONTEXT: ') ||
      line.startsWith('[Project context: ') ||
      line.startsWith('[Project: "') ||
      line.startsWith('[Chat: "')
    ) {
      seenContext = true
      idx += 1
      continue
    }
    if (line.startsWith('[PWA interface: ')) {
      seenContext = true
      idx += 1
      while (idx < lines.length) {
        if (lines[idx].endsWith('space.]')) {
          idx += 1
          break
        }
        idx += 1
      }
      continue
    }
    break
  }

  if (seenContext) {
    while (idx < lines.length && !lines[idx].trim()) idx += 1
    const remainder = lines.slice(idx).join('\n').trim()
    return remainder || content
  }

  return content
}

// Mirror of ciao/web/routes_api.py:_IMAGE_MANIFEST_RE. `build_prompt()` in
// ciao/providers/base.py appends an "[INCOMING IMAGES]\n1. filename.png"
// manifest to the user's text before sending to the SDK. The SDK persists
// it in the session file, so it leaks into replayed history. The UI renders
// images separately from `msg.images`, so the manifest is redundant.
const IMAGE_MANIFEST_RE = /\n{0,2}\[INCOMING IMAGES\]\n(?:\d+\. [^\n]*(?:\n|$))+\s*$/

export function stripImageManifest(content: string): string {
  const stripped = content.replace(IMAGE_MANIFEST_RE, '')
  return stripped || content
}

export function sanitizeInjectedContext(content: string): string {
  const beginMarker = '[CIAO_CONTEXT_BEGIN]\n'
  const endMarker = '\n[CIAO_CONTEXT_END]\n\n'
  if (content.startsWith(beginMarker)) {
    const endIndex = content.indexOf(endMarker)
    if (endIndex >= 0) {
      const stripped = content.slice(endIndex + endMarker.length).trim()
      return stripImageManifest(stripped).trim() || content
    }
  }
  const legacy = stripImageManifest(stripLegacyContextPrefix(content))
  return legacy.trim() || content
}

export function normalizeMessages(chatMessages: ChatMessage[]): ChatMessage[] {
  return chatMessages
    .map((message) => {
      let content = message.content || ''
      if (message.role === 'user') content = sanitizeInjectedContext(content)
      content = content.trim()
      return { ...message, content }
    })
    .filter((message) => {
      // Remove cached bubbles written by older clients. The live proxy event
      // now drives one ephemeral connection card outside chat history.
      if (
        message.role === 'system'
        && isHostConnectionUnavailableMessage(
          message.content.replace(/^Error:\s*/i, ''),
        )
      ) return false
      if (message.tool_name === '_activity') return Boolean(message.content)
      if (message.tool_name === '_filecard') {
        return Boolean(message.file_path) && isPlausibleFilePath(message.file_path || '')
      }
      if (message.role === 'system') return Boolean(message.content)
      return Boolean(message.content)
    })
}

export function userMessageIncludesQueuedText(content: string, queuedText: string): boolean {
  const queued = queuedText.trim()
  if (!queued) return false
  const rendered = content.trim()
  if (rendered === queued) return true
  return rendered.split(/\n{2,}/).some(part => part.trim() === queued)
}

export function queuedTextAlreadyRendered(chatMessages: ChatMessage[], queuedText: string): boolean {
  return chatMessages.some(
    m => m.role === 'user' && userMessageIncludesQueuedText(m.content, queuedText),
  )
}

export function historySignature(chatMessages: ChatMessage[]): string {
  return JSON.stringify(
    chatMessages
      .filter(m => m.tool_name !== '_thinking')
      .map((message) => ({
        role: message.role,
        content: message.content,
        tool_name: message.tool_name || '',
        is_error: Boolean(message.is_error),
        phase: message.phase || '',
      }))
  )
}

// The server rebuilds /api/chats/:id/messages from the raw SDK session
// file, which preserves role/content/tools but NOT the ResultEvent
// metadata (usage, effective_model, is_error). When loadMessages adopts
// the server version, overlay that metadata from matching local
// messages so post-reconcile the context % (context_pct lives inside
// usage) doesn't evaporate.
export function mergeMessageFields(sMsg: ChatMessage, lMsg: ChatMessage): ChatMessage {
  const merged: ChatMessage = { ...sMsg }
  if (lMsg.usage && !sMsg.usage) merged.usage = lMsg.usage
  if (lMsg.quota && !sMsg.quota) merged.quota = lMsg.quota
  if (lMsg.effective_model && !sMsg.effective_model) merged.effective_model = lMsg.effective_model
  if (lMsg.is_error !== undefined && sMsg.is_error === undefined) merged.is_error = lMsg.is_error
  if (lMsg.turn_index != null && sMsg.turn_index == null) merged.turn_index = lMsg.turn_index
  if (lMsg.duration_ms != null && sMsg.duration_ms == null) merged.duration_ms = lMsg.duration_ms
  // Loop/schedule marker observed live but missing on the server row (older
  // servers, or a row built before the turn was recorded) — keep the ↻.
  if (lMsg.unattended && !sMsg.unattended) merged.unattended = lMsg.unattended
  if (!merged.timestamp && lMsg.timestamp) merged.timestamp = lMsg.timestamp
  return merged
}

export function groupIntoTurns(msgsList: ChatMessage[]): { user: ChatMessage | null; responses: ChatMessage[] }[] {
  const turns: { user: ChatMessage | null; responses: ChatMessage[] }[] = []
  let currentTurn: { user: ChatMessage | null; responses: ChatMessage[] } = { user: null, responses: [] }
  for (const m of msgsList) {
    if (m.role === 'user') {
      if (currentTurn.user || currentTurn.responses.length) {
        turns.push(currentTurn)
      }
      currentTurn = { user: m, responses: [] }
    } else {
      currentTurn.responses.push(m)
    }
  }
  if (currentTurn.user || currentTurn.responses.length) {
    turns.push(currentTurn)
  }
  return turns
}

export function mergeMetadata(server: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const serverTurns = groupIntoTurns(server)
  const localTurns = groupIntoTurns(local)
  const mergedMessages: ChatMessage[] = []

  for (let i = 0; i < serverTurns.length; i++) {
    const sTurn = serverTurns[i]
    const lTurn = localTurns[i]
    const matches = lTurn && (
      (!sTurn.user && !lTurn.user) ||
      (sTurn.user && lTurn.user && sTurn.user.content === lTurn.user.content)
    )

    if (!matches) {
      if (sTurn.user) mergedMessages.push(sTurn.user)
      mergedMessages.push(...sTurn.responses)
    } else {
      if (sTurn.user && lTurn.user) {
        mergedMessages.push(mergeMessageFields(sTurn.user, lTurn.user))
      }

      const mergedResponses: ChatMessage[] = []
      const sAssistantMsgs = sTurn.responses.filter(m => m.role === 'assistant' && !m.tool_name)
      let sAsstIdx = 0

      for (const lMsg of lTurn.responses) {
        if (lMsg.role === 'assistant' && !lMsg.tool_name) {
          const sMsg = sAssistantMsgs[sAsstIdx]
          if (sMsg) {
            mergedResponses.push(mergeMessageFields(sMsg, lMsg))
            sAsstIdx++
          }
        } else {
          mergedResponses.push(lMsg)
        }
      }
      for (let j = sAsstIdx; j < sAssistantMsgs.length; j++) {
        mergedResponses.push(sAssistantMsgs[j])
      }
      mergedMessages.push(...mergedResponses)
    }
  }
  return mergedMessages
}

export type ServerRow = { role: string; content: string; tool_name?: string; images?: string[]; turn_index?: number; sent_at?: string; duration_ms?: number; is_error?: boolean; file_path?: string; action?: string; tool?: string; phase?: 'commentary' | 'final_answer'; i?: number; lazy?: boolean; full_length?: number; unattended?: boolean; usage?: Record<string, string>; quota?: Record<string, unknown>; effective_model?: string }

export const toChatMessage = (m: ServerRow) => ({
  role: m.role as 'user' | 'assistant' | 'system',
  content: m.content,
  // sent_at is the persisted send-time (user) or completion-time
  // (assistant) recorded at the orchestration layer. Empty string for
  // pre-feature chats — the renderer treats it as "no time".
  timestamp: m.sent_at || '',
  tool_name: m.tool_name,
  images: m.images,
  // Preserve server-assigned turn_index so user_echo replays (from WS
  // reconnect mid-turn or right after) can dedup against hydrated
  // history. Dropping this caused duplicate user bubbles: the dedup at
  // the user_echo handler matches by turn_index first, and when every
  // hydrated bubble has turn_index: undefined, the replayed echo falls
  // through to msgs.push and renders a second copy of the same turn.
  turn_index: m.turn_index,
  duration_ms: m.duration_ms,
  is_error: m.is_error,
  // Loop/schedule tick marker (↻). The backend records it per turn at
  // send time; without mapping it here a reload made automated turns
  // read as user-authored.
  unattended: m.unattended || undefined,
  // _filecard fields. Empty/undefined for non-file rows.
  file_path: m.file_path,
  action: m.action,
  tool: m.tool,
  phase: m.phase,
  // Envelope annotations (absolute index + lazy marker). Undefined on
  // legacy flat responses.
  i: m.i,
  lazy: m.lazy,
  full_length: m.full_length,
  // Turn footer facts. The provider session file carries none of these, so
  // the server stitches them on from the durable transcript
  // (`_overlay_transcript_metadata`). Dropping them here left every
  // hydrated turn's footer with only the time and duration — no model, no
  // context %.
  usage: m.usage,
  quota: m.quota,
  effective_model: m.effective_model,
})

/** True for a trace row the client renders from streaming events. */
export function isLiveTraceRow(m: ChatMessage): boolean {
  if (m.role === 'assistant') return true
  return m.role === 'system' && (
    m.tool_name === '_activity'
    || m.tool_name === '_thinking'
    || m.tool_name === '_filecard'
  )
}

/**
 * Drop the client's own rendering of a turn the window has now delivered.
 *
 * The live tail and the server's rows are the same turn in two different
 * shapes: the client streams the trace and then adds ONE merged answer
 * bubble, while the server replays the provider's own text parts as separate
 * rows with the tool groups between them. `sameRow` matches on exact content,
 * so it can pair neither the merged bubble (no server row holds that text)
 * nor a part the provider re-joined differently — and both copies survived,
 * rendering the reply once whole and then again in pieces.
 *
 * The server's copy is authoritative once the turn has settled, which is what
 * a window row for an assistant answer carrying a completion `timestamp` says
 * (only the turn-final row gets one, from `_overlay_assistant_timings`). Until
 * then the live tail is all the user has, so it stays.
 *
 * Settling is judged per turn: the tail is split at user bubbles, and only a
 * turn whose own settled answer arrived in `windowRows` loses its live copies.
 * A queued follow-up can finish before the turn ahead of it was ever
 * reconciled, so one refresh may settle several turns at once, while a
 * follow-up still streaming keeps its trace.
 *
 * Only trace rows go. An un-indexed plain system row is a client-side notice
 * — the failed-send warning `recoverUnackedSend` pushes — with no server
 * counterpart to replace it, and an un-indexed user bubble is handled by the
 * optimistic-bubble pruning instead.
 */
export function dropSupersededLiveTail(
  merged: ChatMessage[], tailStart: number, windowRows: ChatMessage[],
): ChatMessage[] {
  const settledIndices = new Set<number>()
  for (const row of windowRows) {
    if (
      typeof row.i === 'number'
      && row.role === 'assistant'
      && !row.is_error
      && Boolean(row.timestamp)
    ) settledIndices.add(row.i)
  }
  if (!settledIndices.size) return merged
  // An earlier refresh can index an answer before its completion overlay
  // arrives, leaving unmatched live activity ABOVE the indexed extent. Once
  // that answer settles, those copies belong to the same pruning pass too.
  // Keep the merge cursor's tail boundary separate from this trace boundary.
  for (let p = 0; p < tailStart; p++) {
    if (typeof merged[p].i !== 'number' && isLiveTraceRow(merged[p])) {
      tailStart = p
      break
    }
  }
  const kept = merged.slice(0, tailStart)
  let changed = false
  let start = tailStart
  while (start < merged.length) {
    let end = start + 1
    while (end < merged.length && merged[end].role !== 'user') end++
    const turn = merged.slice(start, end)
    const settled = turn.some(
      row => typeof row.i === 'number' && settledIndices.has(row.i),
    )
    if (!settled) {
      kept.push(...turn)
      start = end
      continue
    }
    // A server row carries the turn's usage only once `record_turn` has run;
    // for the turn that just streamed it may still be missing. Carry the live
    // values (and the model that answered) onto the server row that closes the
    // turn, or the footer would lose the turn's cost on that reconcile.
    const carried: Partial<ChatMessage> = {}
    const rows: ChatMessage[] = []
    let target = -1
    for (const row of turn) {
      if (typeof row.i !== 'number' && isLiveTraceRow(row)) {
        if (row.usage) carried.usage = row.usage
        if (row.effective_model) carried.effective_model = row.effective_model
        if (row.quota) carried.quota = row.quota
        changed = true
        continue
      }
      if (row.role === 'assistant' && typeof row.i === 'number') target = rows.length
      rows.push(row)
    }
    if (target >= 0 && Object.keys(carried).length) {
      // Fill only the facts the row is actually missing. A plain spread let
      // an explicitly-undefined key on the server row shadow the carried
      // value and lose the turn's cost again.
      rows[target] = mergeMessageFields(rows[target], carried as ChatMessage)
    }
    kept.push(...rows)
    start = end
  }
  return changed ? kept : merged
}

/**
 * True when a history row can stand as the end of a turn: real answer or
 * error output, which is what the user reads in the transcript.
 *
 * The session file is written as the turn runs, so a `/messages` response can
 * still end on a trace row — provider `phase: commentary` narration, or a
 * tool-backed system row (`_activity`, `_thinking`, `_filecard`, any other
 * tool name). Those render from the live stream, not from history, and they
 * land before the final answer is written. Settling on one ended the
 * post-result retries early and left the reply blank until Activity forced a
 * refetch (#630).
 *
 * Whether an empty transcript counts as settled, and whether the server still
 * owns the turn, stay with the callers: this only reads the row.
 */
export function isSettledHistoryRow(row: ChatMessage | undefined): boolean {
  if (!row) return false
  if (!(row.content || '').trim()) return false
  if (row.role === 'assistant') return row.phase !== 'commentary'
  // A system row with a tool_name is a trace step; without one it is a
  // client-side notice (an error the turn ended on, say).
  return row.role === 'system' && !row.tool_name
}

/** Emoji shown beside a tool name in the activity trace. */
export function toolIcon(name: string): string {
  const icons: Record<string, string> = {
    Read: '\u{1F4D6}',     // 📖
    Edit: '\u270F\uFE0F',   // ✏️
    Write: '\u{1F4DD}',    // 📝
    Bash: '$',
    Grep: '\u{1F50D}',     // 🔍
    Glob: '\u{1F4C2}',     // 📂
    Agent: '\u{1F916}',    // 🤖
    Skill: '\u26A1',       // ⚡
    WebSearch: '\u{1F310}', // 🌐
    WebFetch: '\u{1F310}',  // 🌐
    TaskCreate: '\u2611\uFE0F', // ☑️
    TaskUpdate: '\u2611\uFE0F', // ☑️
  }
  return icons[name] || '\u2699\uFE0F' // ⚙️
}
