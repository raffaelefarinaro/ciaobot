import { describe, expect, it } from 'vitest'
import { readChatDraft, writeChatDraft } from './chatDrafts'

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
    storage.setItem('ciao-chat-drafts', '{"chat-a":42,"chat-b":"valid"}')

    expect(readChatDraft('chat-a', storage)).toBe('')
    expect(readChatDraft('chat-b', storage)).toBe('valid')

    storage.setItem('ciao-chat-drafts', '{not-json')
    expect(readChatDraft('chat-b', storage)).toBe('')
  })
})
