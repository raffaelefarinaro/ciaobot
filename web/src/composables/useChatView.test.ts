// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../stores/projects'
import { useChatView } from './useChatView'
import type { ChatInfo, ChatMessage } from '../lib/types'

const apiGet = vi.hoisted(() => vi.fn().mockResolvedValue([]))
vi.mock('../lib/api', () => ({
  api: { get: apiGet, post: vi.fn().mockResolvedValue({}), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('../router', () => ({
  router: { push: vi.fn(), currentRoute: { value: { params: {}, path: '/' } } },
}))

function chat(id: string): ChatInfo {
  return { chat_id: id, project_id: 'p1', title: id, archived: false, last_activity_at: '', last_read_at: '' } as ChatInfo
}

beforeEach(() => {
  setActivePinia(createPinia())
  apiGet.mockClear()
})

describe('useChatView', () => {
  it('reads one chat regardless of the active one', () => {
    const store = useProjectStore()
    store.chats = [chat('chat-a'), chat('chat-b')]
    store.activeChatId = 'chat-a'
    store.messages['chat-b'] = [{ id: 'm1', role: 'user', content: 'hi' } as unknown as ChatMessage]
    store.streaming['chat-b'] = true
    store.queuedMessages['chat-b'] = [{ id: 'q1', text: 'later' } as never]

    const view = useChatView(() => 'chat-b')

    expect(view.activeMessages.length).toBe(1)
    expect(view.isStreaming).toBe(true)
    expect(view.currentQueued.length).toBe(1)
    expect(view.chat?.chat_id).toBe('chat-b')
    expect(store.isStreaming).toBe(false)
  })

  it('reads staged attachments from the chat bucket', () => {
    const store = useProjectStore()
    store.chats = [chat('chat-a'), chat('chat-b')]
    store.activeChatId = 'chat-a'
    store.addPendingImageRefs('chat-b', ['x.png'])

    const view = useChatView(() => 'chat-b')

    expect(view.pendingImages).toEqual(['x.png'])
    expect(store.pendingImages).toEqual([])
  })
})
