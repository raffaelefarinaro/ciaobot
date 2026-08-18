import { describe, expect, it } from 'vitest'
import {
  clearChatDraft,
  readChatDraft,
  readOrphanCandidates,
  readSentPromptHistory,
  recordSentPrompt,
  writeChatDraft,
} from './chatDrafts'

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }
}

describe('chat drafts', () => {
  it('stores and restores exact text independently for each chat', () => {
    const storage = new MemoryStorage()

    writeChatDraft('chat-a', '  first draft\nnext line  ', storage)
    writeChatDraft('chat-b', 'second draft', storage)

    expect(readChatDraft('chat-a', storage)).toBe('  first draft\nnext line  ')
    expect(readChatDraft('chat-b', storage)).toBe('second draft')
  })

  it('clears only the selected chat draft', () => {
    const storage = new MemoryStorage()
    writeChatDraft('chat-a', 'first', storage)
    writeChatDraft('chat-b', 'second', storage)

    writeChatDraft('chat-a', '', storage)

    expect(readChatDraft('chat-a', storage)).toBe('')
    expect(readChatDraft('chat-b', storage)).toBe('second')
  })

  it('ignores malformed and non-string stored values', () => {
    const storage = new MemoryStorage()
    storage.setItem('ciao-chat-drafts', '{"chat-a":42,"chat-b":"valid","chat-c":{"projectId":"p"}}')

    expect(readChatDraft('chat-a', storage)).toBe('')
    expect(readChatDraft('chat-b', storage)).toBe('valid')
    expect(readChatDraft('chat-c', storage)).toBe('')

    storage.setItem('ciao-chat-drafts', '{not-json')
    expect(readChatDraft('chat-b', storage)).toBe('')
  })

  it('round-trips a projectId alongside the draft text', () => {
    const storage = new MemoryStorage()
    writeChatDraft('chat-a', 'hello', storage, { projectId: 'proj-1' })

    expect(readChatDraft('chat-a', storage)).toBe('hello')
    expect(readOrphanCandidates(new Set(), storage)).toEqual([
      expect.objectContaining({ chatId: 'chat-a', text: 'hello', projectId: 'proj-1' }),
    ])
  })

  it('normalizes a legacy plain-string entry into the object shape', () => {
    const storage = new MemoryStorage()
    storage.setItem('ciao-chat-drafts', '{"chat-a":"legacy text"}')

    expect(readChatDraft('chat-a', storage)).toBe('legacy text')
    const [orphan] = readOrphanCandidates(new Set(), storage)
    expect(orphan).toMatchObject({ chatId: 'chat-a', text: 'legacy text', projectId: '' })
    expect(orphan.updatedAt).toBeGreaterThan(0)
  })

  describe('readOrphanCandidates', () => {
    it('excludes drafts whose chat id is still valid', () => {
      const storage = new MemoryStorage()
      writeChatDraft('chat-a', 'still open', storage, { projectId: 'proj-1' })

      expect(readOrphanCandidates(new Set(['chat-a']), storage)).toEqual([])
    })

    it('excludes drafts older than the recovery window', () => {
      const storage = new MemoryStorage()
      const stale = { text: 'old draft', projectId: 'proj-1', updatedAt: Date.now() - 15 * 24 * 60 * 60 * 1000 }
      storage.setItem('ciao-chat-drafts', JSON.stringify({ 'chat-a': stale }))

      expect(readOrphanCandidates(new Set(), storage)).toEqual([])
    })

    it('excludes empty drafts', () => {
      const storage = new MemoryStorage()
      writeChatDraft('chat-a', 'text', storage)
      clearChatDraft('chat-a', storage)

      expect(readOrphanCandidates(new Set(), storage)).toEqual([])
    })
  })

  it('keeps a deduplicated, bounded sent-prompt history per chat', () => {
    const storage = new MemoryStorage()

    for (let i = 0; i < 51; i++) recordSentPrompt('chat-a', `prompt ${i}`, storage)
    recordSentPrompt('chat-a', 'prompt 20', storage)
    recordSentPrompt('chat-b', 'other chat', storage)

    expect(readSentPromptHistory('chat-a', storage)).toHaveLength(50)
    expect(readSentPromptHistory('chat-a', storage)[0]).toBe('prompt 1')
    expect(readSentPromptHistory('chat-a', storage).at(-1)).toBe('prompt 20')
    expect(readSentPromptHistory('chat-b', storage)).toEqual(['other chat'])
  })

  it('ignores malformed history values', () => {
    const storage = new MemoryStorage()
    storage.setItem('ciao-chat-sent-prompts', '{"chat-a":["valid",42,null]}')

    expect(readSentPromptHistory('chat-a', storage)).toEqual(['valid'])
  })
})

