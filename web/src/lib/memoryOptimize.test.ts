// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

const startFileDiscussion = vi.hoisted(() => vi.fn())
const fetchWorkspaceGuide = vi.hoisted(() => vi.fn())
vi.mock('./fileDiscussion', () => ({ startFileDiscussion }))
vi.mock('./workspaceGuide', () => ({ fetchWorkspaceGuide }))

import { OPTIMIZE_CHAT_TITLE, memoryOptimizeSeed, startMemoryOptimizeChat } from './memoryOptimize'

function fakeStore() {
  return { activeWorkspace: 'work', pushErrorToast: vi.fn() } as unknown as Parameters<typeof startMemoryOptimizeChat>[0]
}

describe('startMemoryOptimizeChat', () => {
  afterEach(() => { vi.clearAllMocks() })

  it('asks to trim both regions, approve first, log every removal and report the new percentages', () => {
    const seed = memoryOptimizeSeed('work/AGENTS.md')
    expect(seed).toContain('`work/AGENTS.md`')
    expect(seed).toContain('`ciao:memory`')
    expect(seed).toContain('`ciao:profile`')
    expect(seed).toContain('merge duplicates')
    expect(seed).toContain('project\'s doc')
    expect(seed).toContain('wait for my approval')
    expect(seed).toContain('`ciao memory update`')
    expect(seed).toContain('Workspace/Memory-Consolidations.md')
    expect(seed).toContain('by: chat')
    expect(seed.lastIndexOf('ciao memory status')).toBeGreaterThan(seed.indexOf('ciao memory update'))
  })

  it('opens a draft chat with the workspace guide pinned', async () => {
    fetchWorkspaceGuide.mockResolvedValue({ path: 'work/AGENTS.md', content: '', error: '' })
    const store = fakeStore()
    await startMemoryOptimizeChat(store)
    expect(fetchWorkspaceGuide).toHaveBeenCalledWith('work')
    expect(startFileDiscussion).toHaveBeenCalledWith(store, {
      path: 'work/AGENTS.md', seed: memoryOptimizeSeed('work/AGENTS.md'), title: OPTIMIZE_CHAT_TITLE, workspace: 'work',
    })
  })

  it('says so and opens nothing when the workspace has no guide', async () => {
    fetchWorkspaceGuide.mockResolvedValue({ path: '', content: '', error: '' })
    const store = fakeStore()
    expect(await startMemoryOptimizeChat(store, { workspace: 'personal' })).toBeNull()
    expect(startFileDiscussion).not.toHaveBeenCalled()
    expect(store.pushErrorToast).toHaveBeenCalled()
  })
})
