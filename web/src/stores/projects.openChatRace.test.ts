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
