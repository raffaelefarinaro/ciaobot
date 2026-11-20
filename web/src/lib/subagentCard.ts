/* ------------------------------------------------------------------ *
 * One subagent call as one card: the shape the live trace and the
 * history renderer both produce (`ciao/web/transcript_service.py`,
 * `_render_acp_journal`, mirrors these keys and the step limit).
 *
 * A card is keyed by its call's tool_use_id. Its steps are the nested
 * activity lines and file cards of the children that the call launched.
 * Pure helpers, no Vue imports.
 * ------------------------------------------------------------------ */

export type SubagentState = 'running' | 'done' | 'failed'

export type SubagentStep =
  | { kind: 'activity'; line: string; tool_use_id?: string }
  | { kind: 'file'; file_path: string; action: string; tool: string; tool_use_id?: string }

export interface SubagentCard {
  tool_use_id: string
  agent_type: string
  description: string
  state: SubagentState
  /** Every distinct step the call has seen. History rows carry at most
   *  `SUBAGENT_STEP_LIMIT` of them; the live card may keep more in memory. */
  step_count: number
  steps: SubagentStep[]
}

/** Nested rows drawn per card; the rest are a "+N more" count. */
export const SUBAGENT_STEP_LIMIT = 50

/** The tool names a subagent call travels under: OpenCode's `subagent`,
 *  Claude's `Agent` (and the older `Task`). */
const SUBAGENT_TOOLS = new Set(['agent', 'subagent', 'task'])

export function isSubagentCallName(name: string): boolean {
  return SUBAGENT_TOOLS.has(name.trim().toLowerCase())
}

const INPUT_RE = /^\[([^\]]*)\]\s*([\s\S]*)$/

/** agent type and description from a journalled `[type] description` input.
 *  Older journals kept the call's title (`subagent`) as the input: that names
 *  no description, so the card reads just "Subagent". */
export function subagentFields(toolInput: string): { agent_type: string; description: string } {
  const text = toolInput.trim()
  if (!text || isSubagentCallName(text)) return { agent_type: '', description: '' }
  const match = INPUT_RE.exec(text)
  if (match) return { agent_type: match[1].trim(), description: match[2].trim() }
  return { agent_type: '', description: text }
}

export function newSubagentCard(toolUseId: string, toolInput: string): SubagentCard {
  return { tool_use_id: toolUseId, ...subagentFields(toolInput), state: 'running', step_count: 0, steps: [] }
}

/** A re-announced call with a description fills in or replaces the card's. */
export function rememberSubagentCall(card: SubagentCard, toolInput: string): void {
  const fields = subagentFields(toolInput)
  if (!fields.description) return
  card.agent_type = fields.agent_type
  card.description = fields.description
}

/** The identity of a child's step: its call, and for a file card the file.
 *  Steps without a call id are never merged. */
export function subagentStepKey(step: SubagentStep): string | null {
  if (!step.tool_use_id) return null
  return step.kind === 'file' ? `${step.tool_use_id}\n${step.file_path}` : step.tool_use_id
}

/** Add a child's step, or replace the step the same child call already wrote. */
export function addSubagentStep(card: SubagentCard, step: SubagentStep): void {
  const key = subagentStepKey(step)
  const at = key === null ? -1 : card.steps.findIndex(existing => subagentStepKey(existing) === key)
  if (at >= 0) card.steps[at] = step
  else card.steps.push(step)
  card.step_count = card.steps.length
}

/** Apply a `tool_result` for the call. A launch acknowledgement (background)
 *  leaves the card running; a terminal subagent_status settles it; an error
 *  fails it; any other result is the call's completion. */
export function settleSubagentCard(
  card: SubagentCard,
  result: { background?: boolean; is_error?: boolean; subagent_status?: string },
): void {
  if (result.subagent_status) {
    card.state = result.subagent_status === 'completed' ? 'done' : 'failed'
  } else if (result.is_error) {
    card.state = 'failed'
  } else if (!result.background) {
    card.state = 'done'
  }
}

/** The header text: "Subagent: <description>", or "Subagent" with none. */
export function subagentTitle(card: Pick<SubagentCard, 'description'>): string {
  return card.description ? `Subagent: ${card.description}` : 'Subagent'
}

/** Wire-shaped card from a history row, tolerant of journals written before
 *  the card existed: missing fields take their defaults, unknown states read
 *  as running, and a row without an id is not a card. */
export function subagentCardFromHistory(raw: unknown): SubagentCard | null {
  if (!raw || typeof raw !== 'object') return null
  const data = raw as Record<string, unknown>
  if (typeof data.tool_use_id !== 'string') return null
  const steps = Array.isArray(data.steps)
    ? data.steps.filter((step): step is SubagentStep => Boolean(step) && typeof step === 'object')
      .slice(0, SUBAGENT_STEP_LIMIT)
    : []
  const state: SubagentState = data.state === 'done' || data.state === 'failed' ? data.state : 'running'
  return {
    tool_use_id: data.tool_use_id,
    agent_type: typeof data.agent_type === 'string' ? data.agent_type : '',
    description: typeof data.description === 'string' ? data.description : '',
    state,
    step_count: typeof data.step_count === 'number' ? data.step_count : steps.length,
    steps,
  }
}
