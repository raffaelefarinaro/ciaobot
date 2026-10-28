// @vitest-environment jsdom
//
// Opening a chat whose first turn is already running (an engine-started turn
// such as startup triage, right after an engine restart). The engine's
// buffered `user_echo` is the only source of the user row until the provider
// history is readable, and `/messages` for an opencode chat has to start a
// short-lived `opencode serve`, so it can take seconds.
import { beforeEach, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from './projects'

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api', () => ({
  api: { get: apiGet, post: vi.fn().mockResolvedValue({}), patch: vi.fn(), del: vi.fn() },
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
  // Like the real handshake as far as the store cares: onopen fires synchronously in this setter.
  set onopen(fn: (() => void) | null) { fn?.() }
  get onopen() { return null }
  constructor(public url: string) { sockets.push(this) }
  close() {}
}
let sockets: FakeWebSocket[] = []

beforeEach(() => {
  setActivePinia(createPinia())
  sockets = []
  apiGet.mockReset()
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

test('opening a chat mid first turn attaches the chat socket without waiting for slow /messages', async () => {
  const chatId = 'triage-chat'
  // /messages hangs (opencode serve cold start); every other read resolves.
  let releaseMessages: (v: unknown) => void = () => {}
  apiGet.mockImplementation((url: string) =>
    url.includes('/messages')
      ? new Promise(resolve => { releaseMessages = resolve })
      : Promise.resolve({}),
  )

  const store = useProjectStore()
  // The events socket already told us the turn is running.
  store.projectStreaming[chatId] = true

  void store.switchChat(chatId)
  await vi.waitFor(() => expect(apiGet.mock.calls.some(([u]) => String(u).includes('/messages'))).toBe(true))

  // While /messages is still pending the user is looking at the chat. The
  // broker's buffered user_echo must be able to reach it now: the socket
  // has to be open before history resolves.
  expect(sockets.map(s => s.url)).toEqual([`ws://localhost:3000/ws/chat/${chatId}`])

  releaseMessages({ items: [], total: 0, offset: 0, limit: 50, hasMore: false, nextOffset: null })
})

test('an echo delivered while /messages is pending survives the empty history response', async () => {
  const chatId = 'triage-chat-2'
  let releaseMessages: (v: unknown) => void = () => {}
  apiGet.mockImplementation((url: string) =>
    url.includes('/messages')
      ? new Promise(resolve => { releaseMessages = resolve })
      : Promise.resolve({}),
  )
  const store = useProjectStore()
  store.projectStreaming[chatId] = true

  const opened = store.switchChat(chatId)
  await vi.waitFor(() => expect(sockets).toHaveLength(1))
  sockets[0].onmessage?.({ data: JSON.stringify({
    type: 'user_echo', text: 'Startup triage prompt', origin: 'schedule', turn_index: 0,
  }) })

  releaseMessages({ items: [], total: 0, offset: 0, limit: 50, hasMore: false, nextOffset: null })
  await opened

  const users = (store.messages[chatId] || []).filter(m => m.role === 'user')
  expect(users).toHaveLength(1)
  expect(users[0].origin).toBe('schedule')
})

test('an echo and the history row for the same turn leave exactly one user bubble', async () => {
  const chatId = 'triage-chat-3'
  const prompt = 'Startup triage prompt'
  let releaseMessages: (v: unknown) => void = () => {}
  apiGet.mockImplementation((url: string) =>
    url.includes('/messages')
      ? new Promise(resolve => { releaseMessages = resolve })
      : Promise.resolve({}),
  )
  const store = useProjectStore()
  store.projectStreaming[chatId] = true

  const opened = store.switchChat(chatId)
  await vi.waitFor(() => expect(sockets).toHaveLength(1))
  sockets[0].onmessage?.({ data: JSON.stringify({
    type: 'user_echo', text: prompt, origin: 'schedule', turn_index: 0,
  }) })

  // The merge pairs the server row with the echoed bubble by turn_index. If
  // that pairing broke, the row would be appended next to the echo (two bubbles).
  releaseMessages({
    items: [{ role: 'user', content: prompt, timestamp: '2026-10-28T16:18:00Z', i: 0, turn_index: 0, origin: 'schedule' }],
    total: 1, offset: 0, limit: 50, hasMore: false, nextOffset: null,
  })
  await opened

  const users = (store.messages[chatId] || []).filter(m => m.role === 'user')
  expect(users).toHaveLength(1)
  expect(users[0].origin).toBe('schedule')
})

function bootApi(archived: boolean, onMessages: (release: (v: unknown) => void) => void) {
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve({ workspaces: [], active: 'home', provider_options: [] })
    if (path === '/api/projects') {
      return Promise.resolve([
        { project_id: 'p1', name: 'General', workspace: 'home', context: '', created_at: '', order: 0, vault_folder: 'general' },
      ])
    }
    const chat = { chat_id: 'c1', project_id: 'p1', title: 'Boot', model: '', provider: 'opencode', mode: '', session_id: '', created_at: '', archived }
    if (path === '/api/chats?active_only=1') return Promise.resolve(archived ? [] : [chat])
    if (path === '/api/chats/c1') return Promise.resolve(chat)
    if (path.startsWith('/api/chats/c1/messages')) return new Promise(resolve => onMessages(resolve))
    return Promise.resolve([])
  })
}

test('boot on a chat route opens the chat socket while /messages is still pending', async () => {
  window.history.replaceState({}, '', '/chat/c1')
  try {
    let release: (v: unknown) => void = () => {}
    bootApi(false, r => { release = r })
    const store = useProjectStore()
    await store.fetchAll()

    expect(store.activeChatId).toBe('c1')
    expect(store.messageHistoryLoading).toBe(true)
    expect(sockets.map(s => s.url)).toContain('ws://localhost:3000/ws/chat/c1')
    release([])
  } finally {
    window.history.replaceState({}, '', '/')
  }
})

test('boot on an archived chat route opens no chat socket', async () => {
  window.history.replaceState({}, '', '/chat/c1')
  try {
    bootApi(true, () => {})
    const store = useProjectStore()
    await store.fetchAll()

    expect(store.activeChatId).toBe('c1')
    expect(sockets.map(s => s.url).filter(u => u.includes('/ws/chat/'))).toEqual([])
  } finally {
    window.history.replaceState({}, '', '/')
  }
})
