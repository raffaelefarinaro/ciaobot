import { branchLabel, repoName } from './repo'
import type { ProjectRepo, RepoLocation } from './types'

type Repo = ProjectRepo | null | undefined
type Loc = Partial<RepoLocation> | null | undefined

function trimSep(p: string): string { return p.replace(/[\\/]+$/, '') }
export function samePath(a: string, b: string): boolean { return trimSep(a) === trimSep(b) }

/** A location is present only with a non-empty path; "no location" is `{}`. */
function hasLocation(loc: Loc): loc is Partial<RepoLocation> & { path: string } {
  return typeof loc?.path === 'string' && loc.path !== ''
}

/** True only when the project is linked and the chat worked away from its main checkout. */
export function chatLocationDiffers(repo: Repo, loc: Loc): boolean {
  if (!repo || !hasLocation(loc)) return false
  if (loc.is_worktree) return true
  if (!samePath(repo.path, loc.path)) return true
  return (loc.branch ?? '') !== repo.branch
}

/**
 * The tooltip and accessible name of the sidebar's `code` badge. The badge
 * itself is one short word, so the facts live here: the repository, the branch
 * the chat really works on and the folder it works in. Full values, never the
 * shortened branch the visible tag would show. Without a location the chat
 * works in the linked repository's own checkout.
 */
export function sidebarCodeBadgeTitle(repo: ProjectRepo, loc: Loc): string {
  if (!hasLocation(loc)) {
    return `Code mode · ${repoName(repo.path)} · branch ${branchLabel(repo.branch)} · path: ${repo.path}`
  }
  const worktree = !!loc.is_worktree
  const name = repoName(worktree ? repo.path : loc.path)
  const where = worktree
    ? `worktree${loc.exists === false ? ' removed' : ''}: ${loc.path}`
    : `path: ${loc.path}`
  return `Code mode · ${name} · branch ${branchLabel(loc.branch ?? repo.branch)} · ${where}`
}

export interface RepoChip {
  name: string
  branch: string
  isWorktree: boolean
  /** null = unknown (a worktree's dirtiness is not reported). */
  dirty: boolean | null
  path: string
  missing: boolean
}

export function repoChip(repo: Repo, loc: Loc): RepoChip | null {
  if (!repo) return null
  if (hasLocation(loc) && chatLocationDiffers(repo, loc)) {
    return {
      name: repoName(loc.is_worktree ? repo.path : loc.path),
      branch: branchLabel(loc.branch ?? ''),
      isWorktree: !!loc.is_worktree,
      dirty: null,
      path: loc.path,
      missing: false,
    }
  }
  return {
    name: repoName(repo.path),
    branch: branchLabel(repo.branch),
    isWorktree: false,
    dirty: repo.missing ? null : repo.dirty,
    path: repo.path,
    missing: repo.missing,
  }
}
