import { describe, expect, it, vi } from 'vitest'
import { branchLabel, findRepoInstructions, inCodeMode, joinRepoPath, repoName } from './repo'

function fakeFetch(...statuses: number[]) {
  const fn = vi.fn()
  for (const status of statuses) fn.mockResolvedValueOnce({ ok: status >= 200 && status < 300, status })
  return fn as unknown as typeof fetch & ReturnType<typeof vi.fn>
}

describe('repo helpers', () => {
  it('names a repo from POSIX, Windows and trailing-slash paths', () => {
    expect(repoName('/a/b/app/')).toBe('app')
    expect(repoName('C:\\src\\app')).toBe('app')
  })

  it('joins with the separator the path uses', () => {
    expect(joinRepoPath('/r/app/', 'AGENTS.md')).toBe('/r/app/AGENTS.md')
    expect(joinRepoPath('C:\\src\\app', 'AGENTS.md')).toBe('C:\\src\\app\\AGENTS.md')
  })

  it('labels a detached HEAD', () => {
    expect(branchLabel('')).toBe('detached')
    expect(branchLabel('main')).toBe('main')
  })

  it('finds AGENTS.md with an exact probe', async () => {
    const f = fakeFetch(200)
    expect(await findRepoInstructions('/r/app', f)).toBe('/r/app/AGENTS.md')
    const url = (f as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as string
    expect(url).toContain('exact=1')
    expect(url).toContain(encodeURIComponent('/r/app/AGENTS.md'))
  })

  it('falls back to CLAUDE.md', async () => {
    expect(await findRepoInstructions('/r/app', fakeFetch(404, 200))).toBe('/r/app/CLAUDE.md')
  })

  it('returns null when neither exists', async () => {
    expect(await findRepoInstructions('/r/app', fakeFetch(404, 404))).toBeNull()
  })

  it('throws on a non-404 failure', async () => {
    await expect(findRepoInstructions('/r/app', fakeFetch(500))).rejects.toThrow('HTTP 500')
  })
})

describe('inCodeMode', () => {
  const repo = { path: '/r/app', branch: 'main', dirty: false, missing: false }

  it('is on for a project linked to a repository that exists', () => {
    expect(inCodeMode(repo)).toBe(true)
  })

  it('is off without a linked repository', () => {
    expect(inCodeMode(null)).toBe(false)
    expect(inCodeMode(undefined)).toBe(false)
  })

  it('is off when the linked folder is missing', () => {
    expect(inCodeMode({ ...repo, missing: true })).toBe(false)
  })
})
