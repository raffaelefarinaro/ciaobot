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
