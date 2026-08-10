import { describe, expect, it } from 'vitest'
import {
  readThinkingExpanded,
  THINKING_EXPANDED_STORAGE_KEY,
  writeThinkingExpanded,
} from './useThinkingPreference'

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
}

describe('thinking preference', () => {
  it('defaults to expanded and persists the user choice', () => {
    const storage = new MemoryStorage()
    expect(readThinkingExpanded(storage)).toBe(true)

    writeThinkingExpanded(false, storage)
    expect(storage.getItem(THINKING_EXPANDED_STORAGE_KEY)).toBe('false')
    expect(readThinkingExpanded(storage)).toBe(false)

    writeThinkingExpanded(true, storage)
    expect(readThinkingExpanded(storage)).toBe(true)
  })
})
