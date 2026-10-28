import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchSystemPrompt } from './systemPrompt'

describe('fetchSystemPrompt', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('returns the text on 200', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ text: 'be kind', chars: 7 }), { status: 200 })))
    expect(await fetchSystemPrompt()).toBe('be kind')
  })

  it('returns null on 500', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('boom', { status: 500 })))
    expect(await fetchSystemPrompt()).toBeNull()
  })

  it('returns null when fetch throws', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline') }))
    expect(await fetchSystemPrompt()).toBeNull()
  })

  it('returns an empty string for a 200 with no text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ chars: 0 }), { status: 200 })))
    expect(await fetchSystemPrompt()).toBe('')
  })
})
