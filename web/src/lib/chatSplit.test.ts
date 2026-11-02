// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BESIDE_CHAT_KEY, readBesideChat, writeBesideChat } from './chatSplit'

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

describe('chatSplit', () => {
  let storage: MemoryStorage

  beforeEach(() => {
    storage = new MemoryStorage()
    vi.stubGlobal('localStorage', storage)
  })

  it('remembers one chat per workspace', () => {
    writeBesideChat('personal', 'c1')
    writeBesideChat('work', 'c2')
    expect(readBesideChat('personal')).toBe('c1')
    expect(readBesideChat('work')).toBe('c2')
  })

  it('forgets a workspace entry with an empty id and drops the key when empty', () => {
    writeBesideChat('personal', 'c1')
    writeBesideChat('personal', '')
    expect(readBesideChat('personal')).toBe('')
    expect(storage.getItem(BESIDE_CHAT_KEY)).toBeNull()
  })

  it('reads a corrupt or foreign value as nothing', () => {
    storage.setItem(BESIDE_CHAT_KEY, '[1,2]')
    expect(readBesideChat('personal')).toBe('')
    storage.setItem(BESIDE_CHAT_KEY, '{')
    expect(readBesideChat('personal')).toBe('')
  })
})
