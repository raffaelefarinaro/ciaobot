import type { ChatMessage, SubagentTranscript } from './types'

export type TraceOutput = { file_path: string }

export function collectTraceOutputs(
  steps: Pick<ChatMessage, 'tool_name' | 'file_path' | 'content'>[] | undefined,
): TraceOutput[] {
  const seen = new Set<string>()
  const outputs: TraceOutput[] = []
  for (const step of steps || []) {
    if (step.tool_name !== '_filecard') continue
    const filePath = step.file_path || step.content
    if (!filePath || seen.has(filePath)) continue
    seen.add(filePath)
    outputs.push({ file_path: filePath })
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


