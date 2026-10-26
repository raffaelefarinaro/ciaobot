/**
 * Subagent id normalisation.
 *
 * Claude ids arrive bare ("a319…") from the parent session JSONL and the SDK,
 * and prefixed ("agent-a319…") from the local-transcript fallback. Both forms
 * reach the sidebar rows, the `/chat/:chatId/subagent/:agentId` route, and the
 * store's per-agent fetch, so every surface has to normalise before comparing
 * or routing — otherwise the same agent reads as two.
 */
export function bareAgentId(agentId: string): string {
  return agentId.replace(/^agent-/, '')
}

/** True when two ids name the same agent, whichever form each is in. */
export function sameAgent(a: string, b: string): boolean {
  return bareAgentId(a) === bareAgentId(b)
}

/**
 * Route to one subagent's read-only view. Both the sidebar row and the in-chat
 * panel link here, so the shape lives with the normalisation rather than being
 * written out at each of them — a change to the route in `router.ts` otherwise
 * fixes one entry point and silently breaks the other.
 */
export function subagentPath(chatId: string, agentId: string): string {
  return `/chat/${chatId}/subagent/${bareAgentId(agentId)}`
}

/**
 * True for a background shell the engine lists beside the agents
 * (`subagent_type: "shell"`). It is a process, not an agent: it has no
 * transcript and must not be counted or linked as one.
 */
export function isShellRow(sub: { subagent_type?: string }): boolean {
  return sub.subagent_type === 'shell'
}

/** Short display form of an agent id (most are UUIDs). */
export function shortAgentId(agentId: string): string {
  const id = bareAgentId(agentId)
  return id.length > 12 ? `${id.slice(0, 8)}…` : id
}

/** A running subagent's display name: its dispatch description, else its short id. */
export function subagentLabel(sub: { agent_id: string; description?: string }): string {
  return (sub.description || '').trim() || shortAgentId(sub.agent_id)
}
