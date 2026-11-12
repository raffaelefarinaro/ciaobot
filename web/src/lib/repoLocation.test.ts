import { describe, expect, it } from 'vitest'
import {
  chatLocationDiffers,
  repoChip,
  sidebarCodeBadgeTitle,
} from './repoLocation'
import type { ProjectRepo, RepoLocation } from './types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const here: RepoLocation = { path: '/r/app', branch: 'main', is_worktree: false, exists: true }

describe('repoLocation', () => {
  it('never reports a location for an unlinked project', () => {
    const loc = { path: '/r/app-wt', branch: 'x', is_worktree: true, exists: true }
    expect(chatLocationDiffers(null, loc)).toBe(false)
    expect(chatLocationDiffers(undefined, loc)).toBe(false)
  })

  it('does not differ without a location', () => {
    expect(chatLocationDiffers(repo, {})).toBe(false)
    expect(chatLocationDiffers(repo, null)).toBe(false)
    expect(chatLocationDiffers(repo, undefined)).toBe(false)
  })

  it('does not differ on the main checkout and branch', () => {
    expect(chatLocationDiffers(repo, here)).toBe(false)
    expect(chatLocationDiffers(repo, { ...here, path: '/r/app/' })).toBe(false)
  })

  it('differs for a worktree even on the same branch, and for another branch', () => {
    expect(chatLocationDiffers(repo, { ...here, is_worktree: true, path: '/r/app-wt' })).toBe(true)
    expect(chatLocationDiffers(repo, { ...here, branch: 'feat-x' })).toBe(true)
  })

  it('labels a detached HEAD in the chip', () => {
    expect(repoChip(repo, { ...here, branch: '' })?.branch).toBe('detached')
  })

  it('chip uses the project repo name for a worktree and hides dirtiness', () => {
    const chip = repoChip({ ...repo, dirty: true }, { path: '/r/app-wt', branch: 'feat', is_worktree: true, exists: true })
    expect(chip).toMatchObject({ name: 'app', branch: 'feat', isWorktree: true, dirty: null, path: '/r/app-wt' })
  })

  it('chip for a missing folder', () => {
    const chip = repoChip({ ...repo, missing: true }, {})
    expect(chip).toMatchObject({ missing: true, dirty: null, name: 'app' })
    expect(repoChip(null, here)).toBeNull()
  })

  it('names the full repository branch and worktree in the code badge tooltip', () => {
    const title = sidebarCodeBadgeTitle(repo, {
      path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true,
    })
    expect(title).toBe('Code mode · app · branch feat-x · worktree: /r/app-wt')
    expect(title).toContain('feat-x')
    expect(title).toContain('/r/app-wt')
  })

  it('names the main checkout in the code badge tooltip', () => {
    expect(sidebarCodeBadgeTitle(repo, here)).toBe('Code mode · app · branch main · path: /r/app')
    expect(sidebarCodeBadgeTitle(repo, null)).toBe('Code mode · app · branch main · path: /r/app')
    // A location without a path is no location: the linked checkout, no
    // invented worktree.
    expect(sidebarCodeBadgeTitle(repo, { is_worktree: true })).toBe('Code mode · app · branch main · path: /r/app')
  })

  it('marks a removed worktree in the code badge tooltip', () => {
    const title = sidebarCodeBadgeTitle(repo, {
      path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: false,
    })
    expect(title).toBe('Code mode · app · branch feat-x · worktree removed: /r/app-wt')
  })
})
