// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { shouldReconnectActiveChatOnStreamingStarted, useProjectStore } from './projects'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  api: {
    get: apiGet,
    post: apiPost,
    patch: vi.fn(),
    del: apiDel,
  },
}))

const routerPush = vi.hoisted(() => vi.fn())
vi.mock('../router', () => ({
  router: {
    push: routerPush,
    currentRoute: {
      value: {
        params: {}
      }
    }
  }
}))

class FakeWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  readyState = FakeWebSocket.OPEN
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(public url: string) {
    fakeSockets.push(this)
  }

  send = vi.fn()

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }
}

let fakeSockets: FakeWebSocket[] = []
let localStorageData: Record<string, string> = {}

beforeEach(() => {
  setActivePinia(createPinia())
  fakeSockets = []
  localStorageData = {}
  apiGet.mockReset()
  const storage = {
    getItem: vi.fn((key: string) => localStorageData[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { localStorageData[key] = value }),
    removeItem: vi.fn((key: string) => { delete localStorageData[key] }),
    clear: vi.fn(() => { localStorageData = {} }),
  }
  vi.stubGlobal('localStorage', storage)
  Object.defineProperty(window, 'localStorage', { value: storage, configurable: true })
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

describe('streaming started reconnect guard', () => {
  test('does not reconnect when the active chat socket is already open', () => {
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 1 })).toBe(false)
  })

  test('does not reconnect while the active chat socket is still connecting', () => {
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 0 })).toBe(false)
  })

  test('reconnects only when no usable active chat socket exists', () => {
    expect(shouldReconnectActiveChatOnStreamingStarted(undefined)).toBe(true)
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 2 })).toBe(true)
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 3 })).toBe(true)
  })
})

describe('queued message replay handling', () => {
  test('clears local queued chips when server history contains the flushed user turn', async () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.queuedMessages[chatId] = [{ text: 'msg A' }]
    apiGet.mockResolvedValue([
      { role: 'user', content: 'initial', sent_at: '', turn_index: 0 },
      { role: 'assistant', content: 'reply', sent_at: '' },
      { role: 'user', content: 'msg A', sent_at: '', turn_index: 1 },
    ])

    await store.loadMessages(chatId)

    expect(store.queuedMessages[chatId]).toBeUndefined()
  })

  test('ignores stale queued replay when the flushed combined user turn is already hydrated', () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.messages[chatId] = [
      { role: 'user', content: 'initial', timestamp: '', turn_index: 0 },
      { role: 'assistant', content: 'reply', timestamp: '' },
      { role: 'user', content: 'msg A\n\nmsg B', timestamp: '', turn_index: 1 },
    ]

    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'queued', text: 'msg A' }),
    })

    expect(store.queuedMessages[chatId]).toBeUndefined()
  })
})

describe('latest status sync', () => {
  test('hydrates settled active chat history and clears stale streaming state', async () => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    const store = useProjectStore()
    const chatId = 'c-sync'
    store.chats = [
      { chat_id: chatId, project_id: 'p1', title: 'Old title', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'user', content: 'status?', timestamp: '', turn_index: 0 },
    ]
    store.streaming[chatId] = true
    store.streamingText[chatId] = 'partial'

    apiGet.mockImplementation((path: string) => {
      if (path === '/api/chats') {
        return Promise.resolve([
          { chat_id: chatId, project_id: 'p1', title: 'Fresh title', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false, last_activity_at: '2026-07-06T10:00:00Z' },
        ])
      }
      if (path === `/api/chats/${chatId}/messages`) {
        return Promise.resolve([
          { role: 'user', content: 'status?', sent_at: '2026-07-06T09:59:00Z', turn_index: 0 },
          { role: 'assistant', content: 'done', sent_at: '2026-07-06T10:00:00Z' },
        ])
      }
      if (path === `/api/chats/${chatId}/subagents`) return Promise.resolve([])
      return Promise.resolve([])
    })

    await store.syncLatest()

    expect(store.chats.find(c => c.chat_id === chatId)?.title).toBe('Fresh title')
    expect(store.messages[chatId].at(-1)?.content).toBe('done')
    expect(store.streaming[chatId]).toBe(false)
    expect(store.streamingText[chatId]).toBe('')
  })
})

describe('background agents indicator', () => {
  test('tracks the running count and only reconciles on a drop', () => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    // /messages returns a settled assistant turn so the reconcile fired on a
    // drop resolves on its first pass (no lingering timers after teardown).
    apiGet.mockImplementation((path: string) =>
      path.endsWith('/messages')
        ? Promise.resolve([{ role: 'assistant', content: 'ok', sent_at: '' }])
        : Promise.resolve([]),
    )
    const store = useProjectStore()
    const chatId = 'c-bg'
    store.activeChatId = chatId
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    const fire = (remaining: number) =>
      sock.onmessage?.({
        data: JSON.stringify({ type: 'chat_subagents_ready', chat_id: chatId, project_id: 'p1', remaining }),
      })

    fire(2) // initial announcement
    expect(store.backgroundAgents[chatId]).toBe(2)
    fire(3) // a subagent spawned children — still just a badge update
    expect(store.backgroundAgents[chatId]).toBe(3)
    fire(2) // one finished (drop)
    expect(store.backgroundAgents[chatId]).toBe(2)
    fire(0) // all finished
    expect(store.backgroundAgents[chatId]).toBeUndefined()
    expect(store.activeBackgroundAgents).toBe(0)
  })

  test('a new turn keeps the background-agents count (agents outlive turns)', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-bg2'
    store.activeChatId = chatId
    store.backgroundAgents[chatId] = 4
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({ type: 'chat_streaming_started', chat_id: chatId, project_id: 'p1' }),
    })
    expect(store.backgroundAgents[chatId]).toBe(4)
  })

  test('the events snapshot replaces background-agent counts wholesale', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    store.backgroundAgents['c-stale'] = 7
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({
        type: 'snapshot',
        active_streams: [],
        background_agents: { 'c-live': 2 },
      }),
    })
    expect(store.backgroundAgents['c-stale']).toBeUndefined()
    expect(store.backgroundAgents['c-live']).toBe(2)
  })
})

describe('workspace and chat transitions', () => {
  beforeEach(() => {
    routerPush.mockReset()
    apiPost.mockReset()
    apiDel.mockReset()
  })

  test('fetchAll loads configured workspaces and keeps saved custom workspace', async () => {
    window.localStorage.setItem('ciao-active-workspace', 'client')
    const store = useProjectStore()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/workspaces') {
        return Promise.resolve({
          workspaces: [
            { name: 'home', vault_root: 'memory-vault/home', default_provider: 'ollama', default_model: '', gws_profile: 'personal', model_bucket: 'personal' },
            { name: 'client', vault_root: 'vaults/client', default_provider: 'claude', default_model: '', gws_profile: 'work', model_bucket: 'work' },
          ],
          active: 'home',
          provider_options: [
            { value: 'claude', label: 'Claude' },
            { value: 'ollama', label: 'Ollama' },
          ],
        })
      }
      if (path === '/api/projects') {
        return Promise.resolve([
          { project_id: 'p-home', name: 'General', workspace: 'home', context: '', created_at: '', order: 0, vault_folder: 'general' },
          { project_id: 'p-client', name: 'General', workspace: 'client', context: '', created_at: '', order: 0, vault_folder: 'general' },
        ])
      }
      if (path === '/api/chats') {
        return Promise.resolve([
          { chat_id: 'c-client', project_id: 'p-client', title: 'Client chat', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
        ])
      }
      if (path === '/api/chats/c-client/messages') return Promise.resolve([])
      return Promise.resolve([])
    })

    await store.fetchAll()

    expect(store.workspaceOptions.map(w => w.name)).toEqual(['home', 'client'])
    expect(store.activeWorkspace).toBe('client')
    expect(store.activeChatId).toBe('c-client')
  })

  test('switchWorkspace transitions to first chat of new workspace and marks it read', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p-personal', name: 'Proj Personal', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p-work', name: 'Proj Work', workspace: 'work', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c-personal', project_id: 'p-personal', title: 'Chat Personal', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'c-work', project_id: 'p-work', title: 'Chat Work', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeWorkspace = 'work'
    store.activeChatId = 'c-work'

    apiGet.mockResolvedValue([]) // loadMessages mock response

    await store.switchWorkspace('personal')

    expect(store.activeWorkspace).toBe('personal')
    expect(store.activeChatId).toBe('c-personal')
    expect(routerPush).toHaveBeenCalledWith('/chat/c-personal')
    expect(apiPost).toHaveBeenCalledWith('/api/chats/c-personal/read', {})
  })

  test('deleteChat on active chat transitions to first chat of current workspace', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Proj 1', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c1', project_id: 'p1', title: 'Chat 1', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'c2', project_id: 'p1', title: 'Chat 2', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeWorkspace = 'personal'
    store.activeChatId = 'c1'

    apiGet.mockResolvedValue([]) // loadMessages mock response

    await store.deleteChat('c1')

    expect(store.chats.find(c => c.chat_id === 'c1')).toBeUndefined()
    expect(store.activeChatId).toBe('c2')
    expect(routerPush).toHaveBeenCalledWith('/chat/c2')
    expect(apiPost).toHaveBeenCalledWith('/api/chats/c2/read', {})
  })

  test('deleteProject on project with active chat transitions to first chat of workspace', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Proj 1', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p2', name: 'Proj 2', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c1', project_id: 'p1', title: 'Chat 1', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'c2', project_id: 'p2', title: 'Chat 2', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeWorkspace = 'personal'
    store.activeChatId = 'c1'

    apiGet.mockResolvedValue([]) // loadMessages mock response

    await store.deleteProject('p1')

    expect(store.projects.find(p => p.project_id === 'p1')).toBeUndefined()
    expect(store.chats.find(c => c.project_id === 'p1')).toBeUndefined()
    expect(store.activeChatId).toBe('c2')
    expect(routerPush).toHaveBeenCalledWith('/chat/c2')
    expect(apiPost).toHaveBeenCalledWith('/api/chats/c2/read', {})
  })

  test('fixError opens a chat in the active workspace General project seeded with the error log', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'pg', name: 'General', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: 'general', is_auto: true },
      { project_id: 'pother', name: 'General', workspace: 'work', context: '', created_at: '', order: 0, vault_folder: 'general', is_auto: true },
    ]
    store.activeWorkspace = 'personal'
    apiGet.mockResolvedValue([]) // loadMessages / loadSubagents
    apiPost.mockResolvedValue({
      chat_id: 'c-fix', project_id: 'pg', title: 'Fix error', model: '',
      provider: 'claude', mode: '', session_id: '', created_at: '', archived: false,
    })

    vi.useFakeTimers()
    try {
      const chat = await store.fixError({ errorText: 'Error: boom', context: 'I clicked send' })
      expect(chat?.chat_id).toBe('c-fix')
      // The socket opens async (switchChat awaits a dynamic import), so sendMessage
      // defers the first send by 500ms — advance timers to flush it.
      await vi.advanceTimersByTimeAsync(600)
    } finally {
      vi.useRealTimers()
    }

    expect(apiPost).toHaveBeenCalledWith('/api/projects/pg/chats', { title: 'Fix error' })

    // The fix prompt (with the error log + gh-issue fallback) was sent over the WS.
    const sent = fakeSockets.flatMap(s => (s.send as any).mock.calls.map((c: any[]) => String(c[0])))
    const fixMsg = sent.find(m => m.includes('Error: boom'))
    expect(fixMsg).toBeTruthy()
    expect(fixMsg).toContain('gh issue create --repo raffaelefarinaro/ciaobot')
    expect(fixMsg).toContain('I clicked send')
  })

  test('fixError surfaces an error toast when the workspace has no General project', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Proj 1', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.activeWorkspace = 'personal'

    const chat = await store.fixError({ errorText: 'Error: boom' })

    expect(chat).toBeUndefined()
    expect(apiPost).not.toHaveBeenCalled()
    expect(store.toasts.some(t => t.variant === 'error')).toBe(true)
  })
})
