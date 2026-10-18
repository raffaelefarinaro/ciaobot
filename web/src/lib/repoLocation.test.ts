import { describe, expect, it } from 'vitest'
import {
  chatLocationDiffers,
  chatRepoTag,
  projectRepoTag,
  repoChip,
  repoChipLabel,
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
    expect(projectRepoTag({ ...repo, branch: '' })?.branch).toBe('detached')
  })

  it('marks a removed worktree', () => {
    const tag = chatRepoTag(repo, { path: '/r/wt', branch: 'x', is_worktree: true, exists: false })
    expect(tag?.removed).toBe(true)
    expect(tag?.title).toBe('Worktree removed: /r/wt')
  })

  it('project tag reports dirty only when present', () => {
    expect(projectRepoTag(null)).toBeNull()
    expect(projectRepoTag(repo)).toMatchObject({ branch: 'main', dirty: false, missing: false })
    expect(projectRepoTag({ ...repo, dirty: true })?.dirty).toBe(true)
    expect(projectRepoTag({ ...repo, missing: true, dirty: true })).toMatchObject({ dirty: false, missing: true })
  })

  it('chip uses the project repo name for a worktree and hides dirtiness', () => {
    const chip = repoChip({ ...repo, dirty: true }, { path: '/r/app-wt', branch: 'feat', is_worktree: true, exists: true })
    expect(chip).toMatchObject({ name: 'app', branch: 'feat', isWorktree: true, dirty: null, path: '/r/app-wt' })
  })

  it('chip for a missing folder', () => {
    const chip = repoChip({ ...repo, missing: true }, {})
    expect(chip).toMatchObject({ missing: true, dirty: null, name: 'app' })
    expect(repoChipLabel(chip!)).toBe('Repository app, folder missing')
    expect(repoChip(null, here)).toBeNull()
  })

  it('accessible label spells out worktree and changes', () => {
    const wt = repoChip(repo, { path: '/r/wt', branch: 'feat', is_worktree: true, exists: true })!
    expect(repoChipLabel(wt)).toBe('Repository app, branch feat, worktree')
    const dirty = repoChip({ ...repo, dirty: true }, {})!
    expect(repoChipLabel(dirty)).toBe('Repository app, branch main, uncommitted changes')
  })

  it('labels code mode', () => {
    const wt = repoChip(repo, { path: '/r/app-wt', branch: 'feat', is_worktree: true, exists: true })
    expect(repoChipLabel(wt!, true)).toBe('Repository app, branch feat, worktree, code mode on')
    expect(repoChipLabel(wt!)).toBe('Repository app, branch feat, worktree')
    const missing = repoChip({ ...repo, missing: true }, {})
    expect(repoChipLabel(missing!, true)).toBe('Repository app, folder missing, code mode on')
  })
})
