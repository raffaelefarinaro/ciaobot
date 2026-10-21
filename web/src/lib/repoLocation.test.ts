import { describe, expect, it } from 'vitest'
import {
  chatLocationDiffers,
  chatRepoTag,
  repoChip,
  sidebarBranchChip,
  sidebarCodeBadgeTitle,
} from './repoLocation'
import type { ProjectRepo, RepoLocation } from './types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const here: RepoLocation = { path: '/r/app', branch: 'main', is_worktree: false, exists: true }

describe('repoLocation', () => {
  it('never tags a chat in an unlinked project', () => {
    const loc = { path: '/r/app-wt', branch: 'x', is_worktree: true, exists: true }
    expect(chatLocationDiffers(null, loc)).toBe(false)
    expect(chatRepoTag(undefined, loc)).toBeNull()
  })

  it('does not tag a chat without a location', () => {
    expect(chatRepoTag(repo, {})).toBeNull()
    expect(chatRepoTag(repo, null)).toBeNull()
    expect(chatRepoTag(repo, undefined)).toBeNull()
  })

  it('does not tag a chat on the main checkout and branch', () => {
    expect(chatRepoTag(repo, here)).toBeNull()
    expect(chatRepoTag(repo, { ...here, path: '/r/app/' })).toBeNull()
  })

  it('tags a worktree even on the same branch', () => {
    const tag = chatRepoTag(repo, { ...here, is_worktree: true, path: '/r/app-wt' })
    expect(tag).toEqual({ label: '⑂ main', title: 'Worktree: /r/app-wt', removed: false })
  })

  it('tags another branch in the main checkout', () => {
    const tag = chatRepoTag(repo, { ...here, branch: 'feat-x' })
    expect(tag?.label).toBe('⑂ feat-x')
    expect(tag?.title).toBe('/r/app')
  })

  it('labels a detached HEAD', () => {
    expect(chatRepoTag(repo, { ...here, branch: '' })?.label).toBe('⑂ detached')
  })

  it('marks a removed worktree', () => {
    const tag = chatRepoTag(repo, { path: '/r/wt', branch: 'x', is_worktree: true, exists: false })
    expect(tag?.removed).toBe(true)
    expect(tag?.title).toBe('Worktree removed: /r/wt')
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

  it('marks a code-off sidebar branch chip as inactive', () => {
    const wt = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }
    const off = sidebarBranchChip(repo, wt)
    expect(off?.title).toBe('Worktree: /r/app-wt (code mode off)')
    expect(off?.label).toBe('⑂ feat-x')
    expect(off?.removed).toBe(false)
    expect(sidebarBranchChip(repo, here)).toBeNull()
    expect(sidebarBranchChip(repo, { ...wt, exists: false })?.removed).toBe(true)
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
