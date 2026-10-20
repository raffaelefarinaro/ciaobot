/** Last path segment, POSIX or Windows separators, trailing separators ignored. */
export function repoName(path: string): string {
  return path.replace(/[\\/]+$/, '').split(/[\\/]/).filter(Boolean).pop() || path
}

/** Join a file name onto a host path using the separator the path already uses. */
export function joinRepoPath(root: string, file: string): string {
  const sep = root.includes('\\') && !root.includes('/') ? '\\' : '/'
  return root.replace(/[\\/]+$/, '') + sep + file
}

/** `git branch --show-current` prints '' on a detached HEAD. */
export function branchLabel(branch: string): string {
  return branch.trim() || 'detached'
}

/** Same order the engine injects: AGENTS.md, else CLAUDE.md. */
export const REPO_INSTRUCTION_FILES = ['AGENTS.md', 'CLAUDE.md'] as const

/**
 * First instructions file that exists in the repo, or null. Probes with
 * exact=1: without it /api/workspace-file fuzzy-matches a missing file to
 * another AGENTS.md (e.g. the workspace's) and answers 200.
 */
export async function findRepoInstructions(
  root: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  for (const name of REPO_INSTRUCTION_FILES) {
    const path = joinRepoPath(root, name)
    const resp = await fetchImpl(
      `/api/workspace-file?path=${encodeURIComponent(path)}&exact=1`,
      { credentials: 'same-origin' },
    )
    if (resp.ok) return path
    if (resp.status !== 404) throw new Error(`Could not read ${name} (HTTP ${resp.status}).`)
  }
  return null
}
