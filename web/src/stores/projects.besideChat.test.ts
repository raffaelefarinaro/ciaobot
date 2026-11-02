// @vitest-environment jsdom
//
// A second chat kept live beside the active one (ChatLayout's split). The
// store opens its socket and history, marks it read, and widens the
// "is it on screen" gates from the active chat to the viewed pair.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from './projects'
import type { ChatInfo, ProjectInfo } from '../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn().mockResolvedValue({}))
vi.mock('../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))
vi.mock('../router', () => ({
  router: { push: vi.fn(), currentRoute: { value: { params: {}, path: '/' } } },
}))

class FakeWebSocket {
  static OPEN = 1
  readyState = 1
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: unknown = null
  onerror: unknown = null
  send = vi.fn()
  close = vi.fn()
  // Like the real handshake as far as the store cares: onopen fires synchronously in this setter.
  set onopen(fn: (() => void) | null) { fn?.() }
  get onopen() { return null }
  constructor(public url: string) { sockets.push(this) }
}
let sockets: FakeWebSocket[] = []

function socketFor(suffix: string): FakeWebSocket | undefined {
  return sockets.find(s => s.url.endsWith(suffix))
}

function chat(id: string, extra: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: id,
    project_id: 'p1',
    title: id,
    archived: false,
    last_activity_at: '',
    last_read_at: '',
    ...extra,
  } as ChatInfo
}

const project = { project_id: 'p1', name: 'Project 1' } as ProjectInfo

const emptyPage = { items: [], total: 0, offset: 0, limit: 50, hasMore: false, nextOffset: null }

beforeEach(() => {
  setActivePinia(createPinia())
  sockets = []
  apiGet.mockReset()
  apiGet.mockImplementation((url: string) =>
    Promise.resolve(url.includes('/messages') ? emptyPage : []),
  )
  apiPost.mockClear()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
})

function seed() {
  const store = useProjectStore()
  store.projects = [project]
  store.chats = [chat('chat-a'), chat('chat-b'), chat('chat-c')]
  store.activeChatId = 'chat-a'
  return store
}

describe('attachBesideChat', () => {
  it('attaches a second chat: its own socket, its history and a read mark', async () => {
    const store = seed()

    await store.attachBesideChat('chat-b')

    expect(store.besideChatId).toBe('chat-b')
    const chatSockets = sockets.filter(s => s.url.includes('/ws/chat/'))
    expect(chatSockets.map(s => s.url)).toEqual([expect.stringMatching(/\/ws\/chat\/chat-b$/)])
    expect(apiGet.mock.calls.some(([u]) => String(u).includes('/api/chats/chat-b/messages'))).toBe(true)
    expect(apiPost).toHaveBeenCalledWith('/api/chats/chat-b/read', {})
    expect(store.isViewedChat('chat-b')).toBe(true)
  })

  it('refuses the active chat and unknown chats', async () => {
    const store = seed()

    await store.attachBesideChat('chat-a')
    expect(store.besideChatId).toBeNull()

    await store.attachBesideChat('nope')
    expect(store.besideChatId).toBeNull()
    expect(sockets.filter(s => s.url.includes('/ws/chat/'))).toHaveLength(0)
  })

  it('detaching closes only the second chat socket', async () => {
    const store = seed()
    await store.attachBesideChat('chat-b')
    const chatB = socketFor('/ws/chat/chat-b')!

    await store.attachBesideChat(null)

    expect(chatB.close).toHaveBeenCalledTimes(1)
    expect(store.besideChatId).toBeNull()
    expect(store.isViewedChat('chat-b')).toBe(false)
  })

  it('opening the second chat as the main one keeps its socket and clears the second slot', async () => {
    const store = seed()
    await store.attachBesideChat('chat-b')
    const chatB = socketFor('/ws/chat/chat-b')!

    await store.switchChat('chat-b')

    expect(store.besideChatId).toBeNull()
    expect(store.activeChatId).toBe('chat-b')
    expect(chatB.close).not.toHaveBeenCalled()
    expect(sockets.filter(s => s.url.endsWith('/ws/chat/chat-b'))).toHaveLength(1)
  })

  it('a finished turn in the second chat is read, not unread', async () => {
    const store = seed()
    await store.attachBesideChat('chat-b')
    store.connectEventsWs()
    const events = socketFor('/ws/events')!
    apiPost.mockClear()

    events.onmessage?.({ data: JSON.stringify({ type: 'chat_result_ready', chat_id: 'chat-b', snippet: 'done', title: 't' }) })
    await vi.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/chats/chat-b/read', {}))

    expect(store.unread['chat-b']).toBeUndefined()
    expect(store.chatUnread('chat-b')).toBe(0)
  })

  it('a chat that is not on screen is still marked unread', async () => {
    const store = seed()
    await store.attachBesideChat('chat-b')
    store.connectEventsWs()
    const events = socketFor('/ws/events')!

    events.onmessage?.({ data: JSON.stringify({ type: 'chat_result_ready', chat_id: 'chat-c', snippet: 'done', title: 't' }) })

    expect(store.unread['chat-c']).toBe(1)
  })

  it('switching workspace drops the second chat', async () => {
    const store = seed()
    store.activeWorkspace = 'personal'
    await store.attachBesideChat('chat-b')
    const chatB = socketFor('/ws/chat/chat-b')!

    await store.switchWorkspace('work', { transition: false })

    expect(store.besideChatId).toBeNull()
    expect(chatB.close).toHaveBeenCalledTimes(1)
  })
})

describe('the second chat ends with its chat', () => {
  async function attachAndOpenEvents() {
    const store = seed()
    await store.attachBesideChat('chat-b')
    store.connectEventsWs()
    const chatB = socketFor('/ws/chat/chat-b')!
    const events = socketFor('/ws/events')!
    return { store, chatB, events }
  }

  it('a deleted second chat is dropped and its socket closed', async () => {
    const { store, chatB, events } = await attachAndOpenEvents()

    events.onmessage?.({ data: JSON.stringify({ type: 'chat_deleted', chat_id: 'chat-b' }) })

    expect(store.besideChatId).toBeNull()
    expect(store.isViewedChat('chat-b')).toBe(false)
    expect(chatB.close).toHaveBeenCalledTimes(1)
  })

  it('an archived second chat is dropped and its socket closed', async () => {
    const { store, chatB, events } = await attachAndOpenEvents()

    events.onmessage?.({ data: JSON.stringify({ type: 'chat_archived', chat_id: 'chat-b' }) })

    expect(store.besideChatId).toBeNull()
    expect(store.isViewedChat('chat-b')).toBe(false)
    expect(chatB.close).toHaveBeenCalledTimes(1)
  })
})

describe('second-chat socket lifecycle', () => {
  it('moving the second slot closes the old socket and opens exactly one for the new chat', async () => {
    const store = seed()
    await store.attachBesideChat('chat-b')
    const chatB = socketFor('/ws/chat/chat-b')!

    await store.attachBesideChat('chat-c')

    expect(chatB.close).toHaveBeenCalledTimes(1)
    expect(store.besideChatId).toBe('chat-c')
    const chatCSockets = sockets.filter(s => s.url.endsWith('/ws/chat/chat-c'))
    expect(chatCSockets).toHaveLength(1)
    expect(chatCSockets[0].close).not.toHaveBeenCalled()
  })

  it('attaching the same second chat twice opens one socket', async () => {
    const store = seed()

    await store.attachBesideChat('chat-b')
    await store.attachBesideChat('chat-b')

    expect(sockets.filter(s => s.url.endsWith('/ws/chat/chat-b'))).toHaveLength(1)
    expect(socketFor('/ws/chat/chat-b')!.close).not.toHaveBeenCalled()
    expect(store.besideChatId).toBe('chat-b')
  })
})

describe('a failed archive restores the second chat', () => {
  it('re-attaches the second chat with a fresh socket when the archive POST fails', async () => {
    const store = seed()
    await store.attachBesideChat('chat-b')
    const firstSocket = socketFor('/ws/chat/chat-b')!
    apiPost.mockImplementation((url: string) =>
      url.endsWith('/archive') ? Promise.reject(new Error('disk full')) : Promise.resolve({}),
    )

    await expect(store.archiveChat('chat-b')).rejects.toThrow()

    expect(firstSocket.close).toHaveBeenCalledTimes(1)
    expect(store.besideChatId).toBe('chat-b')
    expect(store.isViewedChat('chat-b')).toBe(true)
    const chatBSockets = sockets.filter(s => s.url.endsWith('/ws/chat/chat-b'))
    expect(chatBSockets).toHaveLength(2)
    expect(chatBSockets[1].close).not.toHaveBeenCalled()
  })
})
