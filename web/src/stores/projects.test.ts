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
            { name: 'home', vault_root: 'memory-vault/home', default_model: '', gws_profile: 'personal', model_bucket: 'personal' },
            { name: 'client', vault_root: 'vaults/client', default_model: '', gws_profile: 'work', model_bucket: 'work' },
          ],
          active: 'home',
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
})
