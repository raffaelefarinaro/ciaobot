// The workspace guide (AGENTS.md) the agent reads on every turn. Shared by the
// Memory page's "Always loaded" rail and the chat's Agent context section.

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
 * so it is tried second and will stop appearing once installs have upgraded.
 *
 * After the workspace re-root migration each guide lives under
 * `<workspace>/AGENTS.md`, so a bare basename would let /api/workspace-file's
 * fuzzy lookup silently resolve to the lexicographically-first workspace's
 * guide. The workspace-qualified path is tried first, then the bare basename
 * for installs that have not re-rooted (guide still at the install root).
 */
export async function fetchWorkspaceGuide(workspace: string): Promise<WorkspaceGuide> {
  const qualified = [`${workspace}/AGENTS.md`, `${workspace}/CLAUDE.md`]
  const bare = ['AGENTS.md', 'CLAUDE.md']
  let lastError = ''
  let qualifiedErrored = false
  for (const candidate of [...qualified, ...bare]) {
    // A bare basename can fuzzy-resolve to a DIFFERENT workspace's guide
    // (routes_helpers._resolve_workspace_path anchors relative paths to the
    // primary root), so it is only a legitimate fallback when every
    // workspace-qualified probe genuinely 404'd. If one of them errored we
    // do not know whether this workspace has a guide, and showing another
    // one's — with Open/Discuss/pin acting on it — is worse than showing
    // nothing.
    if (bare.includes(candidate) && qualifiedErrored) break
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
        if (qualified.includes(candidate)) qualifiedErrored = true
        continue
      }
      return { path: candidate, content: await resp.text(), error: '' }
    } catch (e) {
      if (qualified.includes(candidate)) qualifiedErrored = true
      lastError = e instanceof Error ? e.message : String(e)
    }
  }
  return { path: '', content: '', error: lastError }
}
