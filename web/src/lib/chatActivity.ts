import type { ChatMessage, SubagentTranscript } from './types'
import { isPlausibleFilePath } from './filePaths'

export type TraceOutput = { file_path: string; action?: string }

export function collectTraceOutputs(
  steps: Pick<ChatMessage, 'tool_name' | 'file_path' | 'content' | 'action'>[] | undefined,
): TraceOutput[] {
  const seen = new Set<string>()
  const outputs: TraceOutput[] = []
  for (const step of steps || []) {
    if (step.tool_name !== '_filecard') continue
    const filePath = step.file_path || step.content
    if (!filePath || seen.has(filePath)) continue
    // Drop shell false positives like "There" that are not real paths.
    if (!isPlausibleFilePath(filePath)) continue
    seen.add(filePath)
    outputs.push({
      file_path: filePath,
      ...(step.action ? { action: step.action } : {}),
    })
  }
  return outputs
}

export function formatTokenUsage(usage?: Record<string, unknown>): string {
  if (!usage) return ''
  const hasInput = usage.input_tokens !== undefined && usage.input_tokens !== null && usage.input_tokens !== ''
  const hasOutput = usage.output_tokens !== undefined && usage.output_tokens !== null && usage.output_tokens !== ''
  const hasContext = usage.context_pct !== undefined && usage.context_pct !== null && usage.context_pct !== ''

  const formatNum = (val: unknown) => {
    const num = typeof val === 'number' ? val : parseInt(String(val), 10)
    return isNaN(num) ? String(val) : num.toLocaleString('en-US')
  }

  const parts: string[] = []
  if (hasInput) {
    parts.push(`<span class="token-number">${formatNum(usage.input_tokens)}</span> in`)
  }
  if (hasOutput) {
    parts.push(`<span class="token-number">${formatNum(usage.output_tokens)}</span> out`)
  }
  if (hasContext) {
    parts.push(`<span class="context-pct">${String(usage.context_pct)}</span> ctx`)
  }
  if (!parts.length) return ''
  return `Tokens ${parts.join(' · ')}`
}

/** One ordered piece of a rendered turn: either an Activity trace (grouped
 *  tool calls / thinking / commentary) or a standalone assistant answer bubble. */
export type TurnPart =
  | { kind: 'trace'; steps: ChatMessage[] }
  | { kind: 'assistant'; msg: ChatMessage }

/** Assistant prose step (not an Activity marker). Ignores `phase` so Claude
 *  mid-turn narration can still be found before we classify it. */
export function isAssistantTextStep(
  m: Pick<ChatMessage, 'role' | 'tool_name'>,
): boolean {
  return (
    m.role === 'assistant'
    && m.tool_name !== '_activity'
    && m.tool_name !== '_thinking'
    && m.tool_name !== '_filecard'
  )
}

/** True when a buffered step is substantive assistant answer text that should
 *  render as its own bubble — not an Activity marker (`_activity`/`_thinking`/
 *  `_filecard`, all emitted with role `system`) and not Codex `commentary`
 *  narration (which stays folded into the reasoning trace). */
export function isAnswerBubble(
  m: Pick<ChatMessage, 'role' | 'tool_name' | 'phase'>,
): boolean {
  return isAssistantTextStep(m) && m.phase !== 'commentary'
}

/**
 * Heuristic for Claude mid-turn progress narration (Codex already stamps
 * `phase: commentary`). Validated against real Ciao multi-text turns:
 * fold "Now let me…", "Let me…", trailing-colon status lines; keep long
 * updates, blockers/decisions, and answer-shaped openings.
 *
 * Never apply this alone to the last text in a turn — callers must always
 * keep the final block visible (it can be a short clarifying question).
 */
const PROGRESS_OPENER_RE = /^(now\b|let me\b|i['']ll\b|i will\b|looking\b|checking\b|searching\b|reading\b|next\b|okay[,.]?\s*let|ok[,.]?\s*let|alright\b|updating\b|fixing\b|adding\b|writing\b|running\b|making\b|i['']m going\b|i am going\b|digging\b|inspecting\b|opening\b|creating\b|wiring\b|clean[,.]?\b|good[,.]?\b|got it\b)/i
const ANSWER_OPENER_RE = /^(Done|Fixed|Shipped|Merged|Implemented|Here['']s|Here is|Summary|Both moves|Half right)\b/i
const DECISION_RE = /\b(blocked|blocking|need your|your call|before i)\b/i
const MARKDOWN_ANSWER_RE = /^(#{1,3}\s|[-*]\s|\*\*[A-Z])/m

export function isProgressCommentary(content: string): boolean {
  const t = (content || '').trim()
  if (!t) return true

  // Keep substantive mid-turn updates.
  if (t.length >= 200) return false
  // Keep user-facing blockers / decisions at any length (can be short).
  if (DECISION_RE.test(t)) return false
  if (ANSWER_OPENER_RE.test(t)) return false
  if (MARKDOWN_ANSWER_RE.test(t)) return false

  const first = t.split('\n', 1)[0].trim()
  if (/:\s*$/.test(first) && t.length < 200) return true
  if (PROGRESS_OPENER_RE.test(first) && t.length < 250) return true
  return false
}

/** Index of the turn's user-facing final reply. Prefers the last non-progress
 *  assistant text (so a trailing "Now the docs:" after a real answer does not
 *  steal the final bubble); falls back to the last assistant text so a
 *  short clarifying question still surfaces. Returns -1 when none. */
export function findFinalAnswerIndex(
  buffer: Array<Pick<ChatMessage, 'role' | 'tool_name' | 'phase' | 'content'>>,
): number {
  let fallback = -1
  for (let k = buffer.length - 1; k >= 0; k--) {
    const m = buffer[k]
    if (!isAssistantTextStep(m)) continue
    if (m.phase === 'commentary') continue
    if (fallback < 0) fallback = k
    if (m.phase === 'final_answer') return k
    if (!isProgressCommentary(m.content || '')) return k
  }
  return fallback
}

/** True when this assistant text should render as its own bubble (given it is
 *  not the turn-final index, which the caller always keeps). */
export function shouldRenderAnswerBubble(
  m: Pick<ChatMessage, 'role' | 'tool_name' | 'phase' | 'content'>,
): boolean {
  if (!isAssistantTextStep(m)) return false
  if (m.phase === 'commentary') return false
  if (m.phase === 'final_answer') return true
  return !isProgressCommentary(m.content || '')
}

/** Split one turn's buffered steps into ordered parts, EXCLUDING the final
 *  answer bubble at `finalIdx` (the caller appends that itself, with the
 *  turn's outputs/subchats attached).
 *
 *  Substantive assistant text renders as its own message bubble. Claude
 *  progress narration (`Now let me…`, short status lines) and Codex
 *  `phase: commentary` fold into the Activity trace with the tool/thinking
 *  steps between them. Bookkeeping tool calls after the final answer
 *  (`buffer` indices past `finalIdx`) fold into the trace that precedes the
 *  reply, never a dangling block below it.
 *
 *  Pass `finalIdx < 0` when the turn produced no answer bubble (in progress /
 *  interrupted / tools only): every step then groups into traces. */
export function buildTurnParts(buffer: ChatMessage[], finalIdx: number): TurnPart[] {
  const parts: TurnPart[] = []
  let steps: ChatMessage[] = []
  const flush = () => {
    if (steps.length) {
      parts.push({ kind: 'trace', steps })
      steps = []
    }
  }
  for (let k = 0; k < buffer.length; k++) {
    if (k === finalIdx) continue
    const m = buffer[k]
    if (shouldRenderAnswerBubble(m)) {
      flush()
      parts.push({ kind: 'assistant', msg: m })
    } else {
      steps.push(m)
    }
  }
  flush()
  return parts
}

export function traceSummaryMeta(steps: ChatMessage[], subs?: SubagentTranscript[]): string {
  let toolCount = 0
  let textCount = 0
  let thinkingCount = 0
  let fileCount = 0
  for (const s of steps) {
    if (s.tool_name === '_activity') {
      toolCount += s.content.split('\n').filter(Boolean).length
    } else if (s.tool_name === '_thinking') {
      thinkingCount += 1
    } else if (s.tool_name === '_filecard') {
      fileCount += 1
    } else if (s.role === 'assistant') {
      textCount += 1
    }
  }
  const parts: string[] = []
  if (thinkingCount) parts.push(`${thinkingCount} thought${thinkingCount === 1 ? '' : 's'}`)
  if (textCount) parts.push(`${textCount} note${textCount === 1 ? '' : 's'}`)
  if (toolCount) parts.push(`${toolCount} tool call${toolCount === 1 ? '' : 's'}`)
  if (fileCount) parts.push(`${fileCount} file${fileCount === 1 ? '' : 's'}`)
  if (subs?.length) {
    parts.push(`${subs.length} subagent${subs.length === 1 ? '' : 's'}`)
  }
  return parts.join(' · ') || 'steps'
}

export interface MetaPart {
  key: string
  text: string
  shortText?: string
  isImportant?: boolean
}

export function traceSummaryMetaParts(steps: ChatMessage[], subs?: SubagentTranscript[]): MetaPart[] {
  let toolCount = 0
  let textCount = 0
  let thinkingCount = 0
  let fileCount = 0
  for (const s of steps) {
    if (s.tool_name === '_activity') {
      toolCount += s.content.split('\n').filter(Boolean).length
    } else if (s.tool_name === '_thinking') {
      thinkingCount += 1
    } else if (s.tool_name === '_filecard') {
      fileCount += 1
    } else if (s.role === 'assistant') {
      textCount += 1
    }
  }
  const parts: MetaPart[] = []
  if (thinkingCount) {
    parts.push({
      key: 'thoughts',
      text: `${thinkingCount} thought${thinkingCount === 1 ? '' : 's'}`,
      shortText: `${thinkingCount} th`
    })
  }
  if (textCount) {
    parts.push({
      key: 'notes',
      text: `${textCount} note${textCount === 1 ? '' : 's'}`,
      shortText: `${textCount} n`
    })
  }
  if (toolCount) {
    parts.push({
      key: 'tools',
      text: `${toolCount} tool call${toolCount === 1 ? '' : 's'}`,
      shortText: `${toolCount} tool${toolCount === 1 ? '' : 's'}`,
      isImportant: true
    })
  }
  if (fileCount) {
    parts.push({
      key: 'files',
      text: `${fileCount} file${fileCount === 1 ? '' : 's'}`,
      shortText: `${fileCount} f`
    })
  }
  if (subs?.length) {
    parts.push({
      key: 'subagents',
      text: `${subs.length} subagent${subs.length === 1 ? '' : 's'}`,
      shortText: `${subs.length} sub${subs.length === 1 ? '' : 's'}`
    })
  }
  return parts
}


