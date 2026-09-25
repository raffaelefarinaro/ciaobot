import type { ChatMessage, SubagentTranscript } from './types'
import { formatConnectorLabel } from './mcpLabels'
import { isPlausibleFilePath } from './filePaths'

export type TraceOutput = { file_path: string; action?: string }

/** Canonical spelling used to compare two file-card paths.
 *
 * The backend canonicalises a file touch per *tool call*
 * (`chat_broker.normalize_file_touch_paths`), never across a turn, and its
 * canonical form falls back to the raw string whenever the resolved path
 * escapes the workspace root. So one file touched by several calls in one
 * turn can reach the client under several spellings — an absolute Write
 * followed by a workspace-relative Edit, a `./` prefixed shell target, a
 * doubled separator. Keying the Outputs dedup on the raw string let every
 * spelling through, and since the row shows only the basename they rendered
 * as identical duplicate entries (the reported bug).
 */
export function normalizeOutputPath(filePath: string): string {
  let path = (filePath || '').trim().replace(/\\/g, '/')
  path = path.replace(/\/{2,}/g, '/')
  path = path.replace(/^\.\//, '')
  while (path.includes('/./')) path = path.replace(/\/\.\//g, '/')
  return path.replace(/\/+$/, '')
}

/** True when two normalised paths name the same file.
 *
 * Beyond an exact match this accepts exactly one pair: an absolute path and
 * the workspace-relative path it ends with, on a segment boundary. That is
 * the divergence the backend can produce for a single file. Two relative
 * paths are never folded together — `src/a.md` and `docs/src/a.md` are
 * genuinely different files.
 */
function isSameOutputFile(a: string, b: string): boolean {
  if (a === b) return true
  if (a.startsWith('/') === b.startsWith('/')) return false
  const [abs, rel] = a.startsWith('/') ? [a, b] : [b, a]
  return rel.length > 0 && abs.endsWith('/' + rel)
}

/** How much a touch says about what the turn *produced*. The Outputs list
 *  answers "what came out of this turn", so when one file was created and
 *  then edited again the creation is the truthful summary. A rank also keeps
 *  the label stable: the same turn replays in a different order from the live
 *  stream and from reloaded history, so "last touch wins" would flip the
 *  label between the two. */
const OUTPUT_ACTION_RANK: Record<string, number> = {
  created: 3,
  generated: 3,
  written: 2,
  edited: 2,
  surfaced: 1,
  touched: 1,
}

function outputActionRank(action?: string): number {
  if (!action) return 0
  return OUTPUT_ACTION_RANK[action.trim().toLowerCase()] ?? 1
}

/** Short tag rendered next to an output row. Maps the action values the
 *  backend actually emits (`chat_broker._FILE_TOUCH_ACTIONS` plus the
 *  `created` upgrade in `refine_file_touch_actions`); an unknown value falls
 *  through unchanged rather than being invented away. */
const OUTPUT_ACTION_TAGS: Record<string, string> = {
  created: 'new',
  generated: 'new',
  written: 'edited',
  edited: 'edited',
  surfaced: 'shown',
}

export function outputActionTag(action?: string): string {
  const key = (action || '').trim().toLowerCase()
  if (!key) return 'touched'
  return OUTPUT_ACTION_TAGS[key] || key
}

export function collectTraceOutputs(
  steps: Pick<ChatMessage, 'tool_name' | 'file_path' | 'content' | 'action'>[] | undefined,
): TraceOutput[] {
  const keys: string[] = []
  const outputs: TraceOutput[] = []
  for (const step of steps || []) {
    if (step.tool_name !== '_filecard') continue
    const filePath = step.file_path || step.content
    if (!filePath) continue
    // Drop shell false positives like "There" that are not real paths.
    if (!isPlausibleFilePath(filePath)) continue
    const key = normalizeOutputPath(filePath)
    if (!key) continue
    const at = keys.findIndex(k => isSameOutputFile(k, key))
    if (at >= 0) {
      // Same file again. Keep the first spelling — that is the path the
      // reader would have clicked — and upgrade the action if this touch
      // says more about the file than the one already recorded.
      const existing = outputs[at]
      if (step.action && outputActionRank(step.action) > outputActionRank(existing.action)) {
        existing.action = step.action
      }
      continue
    }
    keys.push(key)
    outputs.push({
      file_path: filePath,
      ...(step.action ? { action: step.action } : {}),
    })
  }
  return outputs
}

/** One list of files for a whole chat, from each turn's outputs: the same
 *  file across turns (or spelled absolute vs relative) is one row, keeping
 *  the first spelling and the most telling action. */
export function mergeTraceOutputs(lists: Iterable<TraceOutput[] | undefined>): TraceOutput[] {
  const keys: string[] = []
  const merged: TraceOutput[] = []
  for (const list of lists) {
    for (const output of list ?? []) {
      const key = normalizeOutputPath(output.file_path)
      if (!key) continue
      const at = keys.findIndex(k => isSameOutputFile(k, key))
      if (at >= 0) {
        if (output.action && outputActionRank(output.action) > outputActionRank(merged[at].action)) {
          merged[at] = { ...merged[at], action: output.action }
        }
        continue
      }
      keys.push(key)
      merged.push({ ...output })
    }
  }
  return merged
}

export function formatTokenUsage(usage?: Record<string, unknown>): string {
  if (!usage) return ''
  const inputVal = (usage.input_tokens ?? usage.inputTokens) as unknown
  const outputVal = (usage.output_tokens ?? usage.outputTokens) as unknown
  const contextVal = (usage.context_pct ?? usage.contextPct) as unknown
  const hasInput = inputVal !== undefined && inputVal !== null && inputVal !== ''
  const hasOutput = outputVal !== undefined && outputVal !== null && outputVal !== ''
  const hasContext = contextVal !== undefined && contextVal !== null && contextVal !== ''

  const formatNum = (val: unknown) => {
    const num = typeof val === 'number' ? val : parseInt(String(val), 10)
    return isNaN(num) ? String(val) : num.toLocaleString('en-US')
  }

  const parts: string[] = []
  if (hasInput) {
    parts.push(`<span class="token-number">${formatNum(inputVal)}</span> in`)
  }
  if (hasOutput) {
    parts.push(`<span class="token-number">${formatNum(outputVal)}</span> out`)
  }
  if (hasContext) {
    parts.push(`<span class="context-pct">${String(contextVal)}</span> ctx`)
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
function isAssistantTextStep(
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
 *  `_filecard`, all emitted with role `system`) and not provider `commentary`
 *  narration (which stays folded into the reasoning trace). */
export function isAnswerBubble(
  m: Pick<ChatMessage, 'role' | 'tool_name' | 'phase'>,
): boolean {
  return isAssistantTextStep(m) && m.phase !== 'commentary'
}

/**
 * Heuristic for Claude mid-turn progress narration (some providers stamp
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
function shouldRenderAnswerBubble(
  m: Pick<ChatMessage, 'role' | 'tool_name' | 'phase' | 'content'>,
): boolean {
  if (!isAssistantTextStep(m)) return false
  if (m.phase === 'commentary') return false
  if (m.phase === 'final_answer') return true
  return !isProgressCommentary(m.content || '')
}

/** Split one turn's buffered steps into ordered parts, EXCLUDING the final
 *  answer bubble at `finalIdx` (the caller appends that itself, with the
 *  turn's outputs/subagents attached).
 *
 *  Substantive assistant text renders as its own message bubble. Claude
 *  progress narration (`Now let me…`, short status lines) and
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


/* ------------------------------------------------------------------ *
 * Presentation helpers for one trace row.
 *
 * These were private to `ChatPanel.vue` until the completed-turn Activity
 * row moved into `ChatTurnActivity.vue`; both the live trace and the
 * completed trace render the same rows, so the helpers live here rather
 * than being duplicated or passed down as props.
 * ------------------------------------------------------------------ */

/** Split an `_activity` step's body into the non-empty lines it renders as. */
export function activityLines(content: string): string[] {
  return content.split('\n').map(line => line.trim()).filter(Boolean)
}

/** A trace line a subagent produced: the server prefixes those with `↳`. */
export function isSubagentLine(line: string): boolean {
  return line.trimStart().startsWith('↳')  // ↳
}

/** Images open in the image viewer; everything else (markdown, code, config,
 *  plain text) goes through `open`. Binary formats the viewer doesn't render
 *  (PDF, docx, xlsx, pptx, zip) fall through to `open`, which will 415 and
 *  show a clear error. */
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico|tiff?)$/i

export function isImageFilePath(filePath: string): boolean {
  return IMAGE_EXT_RE.test(filePath)
}

export function fileCardBasename(filePath: string): string {
  if (!filePath) return ''
  const cleaned = filePath.replace(/[/\\]+$/, '')
  const slash = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf('\\'))
  return slash >= 0 ? cleaned.slice(slash + 1) : cleaned
}

export function fileCardDirname(filePath: string): string {
  if (!filePath) return ''
  const slash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))
  return slash > 0 ? filePath.slice(0, slash) : ''
}

/** Subset of `AppIconName` a file card can use. Kept as a literal union so
 *  `lib/` stays free of Vue imports; `AppIcon` accepts all three. */
export type FileCardIcon = 'image' | 'doc' | 'file'

// Emoji cannot inherit currentColor, so file glyphs are SVG names now; see
// docs/DESIGN_SYSTEM.md rule S4.
export function fileCardIcon(filePath: string): FileCardIcon {
  if (isImageFilePath(filePath)) return 'image'
  if (/\.(md|markdown|txt)$/i.test(filePath)) return 'doc'
  if (/\.(pdf|docx?|xlsx?|pptx?)$/i.test(filePath)) return 'doc'
  return 'file'
}


/* ------------------------------------------------------------------ *
 * Tool usage for the chat's Work details rail: which skills and MCP
 * tools a chat's turns called, read from the same activity lines the
 * trace renders ("<icon> <ToolName> <summary>", subagent lines prefixed
 * with ↳). Both providers write that shape; they differ in naming:
 *   Claude Code  Skill <name>          mcp__<server>__<tool>
 *   opencode     skill {"name": ...}   <server>_<tool> (no fixed separator)
 * opencode's MCP names cannot be split into server and tool reliably, so
 * they are reported by their full tool name.
 * ------------------------------------------------------------------ */

export interface UsageEntry {
  name: string
  count: number
  /** For an MCP server: the tools it was called with, most used first. */
  tools?: string[]
}

export interface ToolUsage {
  skills: UsageEntry[]
  mcp: UsageEntry[]
}

// opencode's own tools. Anything else with an underscore is an MCP tool.
const OPENCODE_BUILTIN_TOOLS = new Set([
  'read', 'write', 'edit', 'multiedit', 'patch', 'bash', 'grep', 'glob', 'list',
  'webfetch', 'websearch', 'codesearch', 'todowrite', 'todoread', 'task',
  'skill', 'lsp', 'question', 'invalid', 'batch',
])

function parseToolLine(line: string): { name: string; summary: string } | null {
  const tokens = line.trim().replace(/^↳\s*/, '').split(/\s+/)
  const at = tokens.findIndex(token => /^[A-Za-z_][\w.:-]*$/.test(token))
  if (at < 0) return null
  return { name: tokens[at], summary: tokens.slice(at + 1).join(' ').trim() }
}

function skillNameFrom(summary: string): string {
  if (!summary) return ''
  if (summary.startsWith('{')) {
    try {
      const parsed = JSON.parse(summary) as Record<string, unknown>
      const name = parsed.name ?? parsed.skill
      return typeof name === 'string' ? name.trim() : ''
    } catch {
      return ''
    }
  }
  return summary.split(/\s+/)[0] ?? ''
}

function bump(map: Map<string, number>, key: string): void {
  if (key) map.set(key, (map.get(key) ?? 0) + 1)
}

export function collectToolUsage(lines: Iterable<string>): ToolUsage {
  const skills = new Map<string, number>()
  const mcp = new Map<string, number>()
  const mcpTools = new Map<string, Map<string, number>>()
  const bumpTool = (server: string, tool: string) => {
    const label = formatConnectorLabel(server)
    bump(mcp, label)
    const tools = mcpTools.get(label) ?? new Map<string, number>()
    bump(tools, tool)
    mcpTools.set(label, tools)
  }
  for (const raw of lines) {
    const parsed = parseToolLine(raw)
    if (!parsed) continue
    const { name, summary } = parsed
    if (name === 'Skill' || name === 'skill') {
      bump(skills, skillNameFrom(summary))
      continue
    }
    // Grouped per server: which connector was used is the useful fact; the
    // individual tools are detail, kept for the row's tooltip.
    const claudeMcp = /^mcp__(.+?)__(.+)$/.exec(name)
    if (claudeMcp) {
      bumpTool(claudeMcp[1], claudeMcp[2])
      continue
    }
    if (name.includes('_') && name === name.toLowerCase() && !OPENCODE_BUILTIN_TOOLS.has(name)) {
      // opencode's `<server>_<tool>` has no fixed separator; the first
      // segment is the best available server name.
      const cut = name.indexOf('_')
      bumpTool(name.slice(0, cut), name.slice(cut + 1))
    }
  }
  const toEntries = (map: Map<string, number>): UsageEntry[] =>
    [...map.entries()]
      .map(([entryName, count]) => ({ name: entryName, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
  return {
    skills: toEntries(skills),
    mcp: toEntries(mcp).map(entry => ({
      ...entry,
      tools: toEntries(mcpTools.get(entry.name) ?? new Map()).map(tool => tool.name),
    })),
  }
}

/**
 * Workspace paths a reply names in `code` or as a markdown link target.
 * A delegate or a shell command can write a file the parent turn never
 * records as a file card, and the reply then says where it went; this lets
 * the rail list it as "mentioned" instead of claiming no files exist.
 */
export function mentionedFilePaths(text: string): string[] {
  const found: string[] = []
  const push = (candidate: string) => {
    const value = candidate.trim().replace(/[.,;:]+$/, '')
    if (!value || /^[a-z]+:\/\//i.test(value) || value.startsWith('#')) return
    if (!value.includes('/') || !/\.\w{1,8}$/.test(value)) return
    if (/\s/.test(value)) return
    if (!found.includes(value)) found.push(value)
  }
  for (const match of text.matchAll(/`([^`\n]+)`/g)) push(match[1])
  for (const match of text.matchAll(/\]\(([^)\s]+)\)/g)) push(match[1])
  return found
}


/**
 * A live step in words, from its activity line ("<icon> <Tool> <summary>"),
 * for the in-flight turn's summary row. Covers both providers' tool names;
 * anything unknown falls back to "<Tool> <summary>".
 */
export function describeToolStep(line: string): string {
  const parsed = parseToolLine(line.replace(/[`*]/g, ''))
  if (!parsed) return ''
  const { name, summary } = parsed
  const lower = name.toLowerCase()
  const base = (value: string) => value.split(/[\\/]/).filter(Boolean).pop() || value
  const first = summary.split(/\s+/)[0] || ''
  if (lower === 'read') return first ? `Reading ${base(first)}` : 'Reading a file'
  if (['write', 'edit', 'multiedit', 'patch', 'notebookedit'].includes(lower)) {
    return first ? `Editing ${base(first)}` : 'Editing a file'
  }
  if (lower === 'grep' || lower === 'glob' || lower === 'codesearch') return summary ? `Searching ${summary}` : 'Searching'
  if (lower === 'websearch') return summary ? `Searching the web for ${summary}` : 'Searching the web'
  if (lower === 'webfetch') return summary ? `Reading ${summary}` : 'Reading a web page'
  if (lower === 'bash') return summary || 'Running a command'
  if (lower === 'agent' || lower === 'task') return summary ? `Delegating: ${summary}` : 'Delegating to an agent'
  if (lower === 'skill') {
    const skill = skillNameFrom(summary)
    return skill ? `Using the ${skill} skill` : 'Using a skill'
  }
  if (lower === 'todowrite' || lower === 'taskcreate' || lower === 'taskupdate') return 'Updating the plan'
  // Claude Code's deferred-tool loader: plumbing, not a step worth naming.
  if (lower === 'toolsearch') return 'Loading tools'
  const mcp = /^mcp__(.+?)__(.+)$/.exec(name)
  if (mcp) return `${mcp[1]} · ${mcp[2].replace(/_/g, ' ')}`
  return summary ? `${name} ${summary}` : name
}
