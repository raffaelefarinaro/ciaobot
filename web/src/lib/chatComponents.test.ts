import { describe, expect, it, vi } from 'vitest'
import { prefetchChatPanelForRoute } from './chatComponents'

describe('chat panel route prefetch', () => {
  it('leaves Home and non-chat pages without optional chat code', () => {
    const load = vi.fn(async () => ({}))

    prefetchChatPanelForRoute('/', undefined, load)
    prefetchChatPanelForRoute('/chat', undefined, load)
    prefetchChatPanelForRoute('/settings', undefined, load)

    expect(load).not.toHaveBeenCalled()
  })

  it('starts the shared chat loader for chat and subagent deep links', async () => {
    const load = vi.fn(async () => ({}))

    prefetchChatPanelForRoute('/chat/chat-1', 'chat-1', load)
    prefetchChatPanelForRoute('/chat/chat-1/subagent/agent-1', 'chat-1', load)
    await Promise.resolve()

    expect(load).toHaveBeenCalledTimes(2)
  })
})
