// The workspace guide (AGENTS.md) the agent reads on every turn. Shared by the
// Memory page's "Always loaded" rail and the chat's Always loaded group.

export interface WorkspaceGuide {
  /** The path it was found at ('' when there is none). */
  path: string
  content: string
  /** The last real failure, when every candidate failed; '' for plain "none yet". */
  error: string
}

/** Rough token count for display: the ~4 characters per token rule of thumb. */
export function tokensFor(chars: number): number {
  return Math.ceil(chars / 4) || 0
}

/** "3.1k" / "840" — compact, for a rail row. */
export function formatTokens(tokens: number): string {
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(1).replace(/\.0$/, '')}k` : String(tokens)
}

/**
 * Find `workspace`'s guide. AGENTS.md is the guide; CLAUDE.md is only what an
 * install that has not run the guide migration still has (ciao/workspace_guide.py),
 * so it is tried second.
 *
 * Each guide lives under `<workspace>/AGENTS.md`. The lookup is always
 * workspace-qualified and exact: a bare basename would let /api/workspace-file
 * resolve to another workspace's guide.
 */
export async function fetchWorkspaceGuide(workspace: string): Promise<WorkspaceGuide> {
  const candidates = [`${workspace}/AGENTS.md`, `${workspace}/CLAUDE.md`]
  let lastError = ''
  for (const candidate of candidates) {
    try {
      // `exact=1`: no fuzzy fallback. Without it, asking for
      // `<ws>/AGENTS.md` on a workspace that has no guide yet
      // filename-matches another workspace's and returns it with a 200.
      const resp = await fetch(`/api/workspace-file?exact=1&path=${encodeURIComponent(candidate)}`, { credentials: 'same-origin' })
      if (resp.status === 404) continue
      // Keep trying the remaining candidates rather than giving up on the
      // first non-404: a transient 503 (the engine restarting) on the first
      // name would otherwise blank the result even though a later name would
      // have served it.
      if (!resp.ok) {
        lastError = `Failed to load ${candidate} (HTTP ${resp.status})`
        continue
      }
      return { path: candidate, content: await resp.text(), error: '' }
    } catch (e) {
      lastError = e instanceof Error ? e.message : String(e)
    }
  }
  return { path: '', content: '', error: lastError }
}
