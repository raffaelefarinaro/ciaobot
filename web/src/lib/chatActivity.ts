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

export function formatTokenUsage(usage?: Record<string, any>): string {
  if (!usage) return ''
  const hasInput = usage.input_tokens !== undefined && usage.input_tokens !== null && usage.input_tokens !== ''
  const hasOutput = usage.output_tokens !== undefined && usage.output_tokens !== null && usage.output_tokens !== ''
  if (!hasInput && !hasOutput) return ''

  const formatNum = (val: any) => {
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
  return `Tokens ${parts.join(' · ')}`
}

/** One ordered piece of a rendered turn: either an Activity trace (grouped
 *  tool calls / thinking / commentary) or a standalone assistant answer bubble. */
export type TurnPart =
  | { kind: 'trace'; steps: ChatMessage[] }
  | { kind: 'assistant'; msg: ChatMessage }

/** True when a buffered step is substantive assistant answer text that should
 *  render as its own bubble — not an Activity marker (`_activity`/`_thinking`/
 *  `_filecard`, all emitted with role `system`) and not Codex `commentary`
 *  narration (which stays folded into the reasoning trace). */
export function isAnswerBubble(
  m: Pick<ChatMessage, 'role' | 'tool_name' | 'phase'>,
): boolean {
  return (
    m.role === 'assistant'
    && m.tool_name !== '_activity'
    && m.tool_name !== '_thinking'
    && m.tool_name !== '_filecard'
    && m.phase !== 'commentary'
  )
}

/** Split one turn's buffered steps into ordered parts, EXCLUDING the final
 *  answer bubble at `finalIdx` (the caller appends that itself, with the
 *  turn's outputs/subchats attached).
 *
 *  Every assistant text block renders as its own message bubble, in order, and
 *  the tool/thinking steps that ran between two consecutive text blocks group
 *  into one Activity trace bubble sitting between them — so an agentic turn
 *  reads as "message → what happened next → message" rather than one giant
 *  collapsed trace with the real messages buried inside. Bookkeeping tool calls
 *  the model ran AFTER its final answer (`buffer` indices past `finalIdx`) fold
 *  into the trace that precedes the reply, never a dangling block below it.
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
    if (isAnswerBubble(m)) {
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


