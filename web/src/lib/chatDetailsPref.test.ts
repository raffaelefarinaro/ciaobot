// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'
import { chatDetailsClosed, rememberChatDetails } from './chatDetailsPref'

describe('chatDetailsPref', () => {
  beforeEach(() => {
    const values = new Map<string, string>()
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: {
        getItem: (k: string) => values.get(k) ?? null,
        setItem: (k: string, v: string) => { values.set(k, v) },
        removeItem: (k: string) => { values.delete(k) },
      },
    })
  })

  it('is open until closed, and remembers a close until it is opened again', () => {
    expect(chatDetailsClosed()).toBe(false)
    rememberChatDetails(false)
    expect(chatDetailsClosed()).toBe(true)
    rememberChatDetails(true)
    expect(chatDetailsClosed()).toBe(false)
  })
})
