// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi, type Mock } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { ProjectInfo, ChatInfo } from '../lib/types'
import {
  shouldReconnectActiveChatOnStreamingStarted,
  chatWsReconnectDelayMs,
  isHostConnectionUnavailableMessage,
  useProjectStore,
} from './projects'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const reloadWhenServerReady = vi.hoisted(() => vi.fn(() => Promise.resolve()))

vi.mock('../lib/api', () => ({
  api: {
    get: apiGet,
    post: apiPost,
    patch: apiPatch,
    del: apiDel,
  },
}))

vi.mock('../lib/serverRestart', async () => {
  const actual = await vi.importActual<typeof import('../lib/serverRestart')>('../lib/serverRestart')
  return {
    ...actual,
    reloadWhenServerReady,
  }
})

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

  // The store distinguishes a socket that completed its handshake from one
  // rejected during it (auth 403 / origin reject), and only fast-reconnects
  // the former. This fake connects instantly, so fire onopen as soon as the
  // store attaches its handler; otherwise every simulated drop looks like a
  // failed handshake and takes the slow backoff path.
  #onopen: (() => void) | null = null
  get onopen() { return this.#onopen }
  set onopen(fn: (() => void) | null) {
    this.#onopen = fn
    fn?.()
  }

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
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDel.mockReset()
  reloadWhenServerReady.mockClear()
  const storage = {
    getItem: vi.fn((key: string) => localStorageData[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { localStorageData[key] = value }),
    removeItem: vi.fn((key: string) => { delete localStorageData[key] }),
    clear: vi.fn(() => { localStorageData = {} }),
  }
  vi.stubGlobal('localStorage', storage)
  Object.defineProperty(window, 'localStorage', { value: storage, configurable: true })
  Object.defineProperty(document, 'hasFocus', {
    value: vi.fn(() => true),
    configurable: true,
  })
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

describe('native window focus reporting', () => {
  test('requires both document visibility and key-window focus', () => {
    let focused = true
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      configurable: true,
    })
    Object.defineProperty(document, 'hasFocus', {
      value: vi.fn(() => focused),
      configurable: true,
    })
    const store = useProjectStore()
    store.activeChatId = 'chat-focus'
    store.connectWs('chat-focus')
    const socket = fakeSockets[0]

    focused = false
    window.dispatchEvent(new Event('blur'))
    focused = true
    window.dispatchEvent(new Event('focus'))

    const payloads = socket.send.mock.calls.map(([raw]) => JSON.parse(raw))
    expect(payloads.at(-2)).toEqual({ type: 'focus', focused: false })
    expect(payloads.at(-1)).toEqual({ type: 'focus', focused: true })
  })
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

describe('per-chat WS auto-reconnect', () => {
  test('reconnect delay starts near-immediate then backs off', () => {
    expect(chatWsReconnectDelayMs(1)).toBe(50)
    expect(chatWsReconnectDelayMs(2)).toBe(100)
    expect(chatWsReconnectDelayMs(3)).toBe(200)
    expect(chatWsReconnectDelayMs(10)).toBe(2000)
  })

  test('reconnects the active chat after an unexpected drop', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-drop'
    store.activeChatId = chatId
    store.connectWs(chatId)
    expect(fakeSockets.length).toBe(1)

    vi.useFakeTimers()
    try {
      fakeSockets[0].close() // simulate an unexpected server-side close
      expect(fakeSockets.length).toBe(1) // reconnect is scheduled, not immediate
      await vi.advanceTimersByTimeAsync(60) // first retry ~50ms
    } finally {
      vi.useRealTimers()
    }

    expect(fakeSockets.length).toBe(2) // fresh socket opened (resync + reconnect)
  })

  test('keeps the live Activity timeline frozen across an unexpected drop', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-freeze'
    store.activeChatId = chatId
    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'tool_use', tool_name: 'Read', tool_input: '{}' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'text_delta', text: 'partial answer' }),
    })
    expect(store.streaming[chatId]).toBe(true)
    expect(store.currentStreamingText).toBe('partial answer')
    expect(store.currentTimeline.some(e => e.kind === 'tool' && e.content.includes('Read'))).toBe(true)

    vi.useFakeTimers()
    try {
      fakeSockets[0].close()
      // Still frozen while reconnect is pending — no blank Activity flash.
      expect(store.streaming[chatId]).toBe(true)
      expect(store.currentStreamingText).toBe('partial answer')
      expect(store.currentTimeline.some(e => e.kind === 'tool' && e.content.includes('Read'))).toBe(true)
      await vi.advanceTimersByTimeAsync(60)
    } finally {
      vi.useRealTimers()
    }

    expect(fakeSockets.length).toBe(2)
    // First real frame after reconnect clears the frozen buffer, then applies
    // the replayed event (avoids duplicating the pre-drop timeline).
    fakeSockets[1].onmessage?.({
      data: JSON.stringify({ type: 'tool_use', tool_name: 'Bash', tool_input: '{}' }),
    })
    expect(store.streaming[chatId]).toBe(true)
    expect(store.currentStreamingText).toBe('')
    expect(store.currentTimeline.some(e => e.kind === 'tool' && e.content.includes('Bash'))).toBe(true)
    expect(store.currentTimeline.some(e => e.kind === 'tool' && e.content.includes('Read'))).toBe(false)
  })

  test('does not reconnect after an intentional disconnect', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-intentional'
    store.activeChatId = chatId
    store.connectWs(chatId)

    vi.useFakeTimers()
    try {
      store.disconnectWs(chatId) // e.g. switching chats
      await vi.advanceTimersByTimeAsync(2000)
    } finally {
      vi.useRealTimers()
    }

    expect(fakeSockets.length).toBe(1) // no auto-reconnect
  })

  test('does not reconnect a chat the user is not viewing', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    store.activeChatId = 'other'
    store.connectWs('c-background')

    vi.useFakeTimers()
    try {
      fakeSockets[0].close()
      await vi.advanceTimersByTimeAsync(2000)
    } finally {
      vi.useRealTimers()
    }

    expect(fakeSockets.length).toBe(1) // background chat's socket stays closed
  })
})

describe('deferred message sends', () => {
  test('does not attach a deferred message image to a later send', () => {
    const store = useProjectStore()
    const chatId = 'chat-deferred-attachment'
    store.activeChatId = chatId
    store.pendingImages = ['original-image.png']

    // With no socket yet, the first send is deferred while connectWs opens one.
    store.sendMessage(chatId, 'first message')
    store.sendMessage(chatId, 'continue')

    const sent = (fakeSockets[0].send as Mock).mock.calls
      .map(([raw]) => JSON.parse(String(raw)))
      .find(payload => payload.type === 'message' && payload.text === 'continue')

    expect(sent).toMatchObject({ type: 'message', text: 'continue' })
    expect(sent.images).toBeUndefined()
  })
})

describe('client host connection failures', () => {
  test('recognizes the legacy proxy error', () => {
    expect(isHostConnectionUnavailableMessage(
      "Host WS unreachable: [Errno 61] Connect call failed ('10.0.0.5', 8443)",
    )).toBe(true)
  })

  test('shows one ephemeral reconnecting state without adding chat errors', () => {
    const store = useProjectStore()
    const chatId = 'c-client-offline'
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'system', content: 'Error: Host WS unreachable: old attempt 1', timestamp: '' },
      { role: 'system', content: 'Error: Host WS unreachable: old attempt 2', timestamp: '' },
      { role: 'user', content: 'keep this', timestamp: '' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'host_unreachable' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'host_unreachable' }),
    })

    expect(store.hostConnectionUnavailable).toBe(true)
    expect(store.messages[chatId]).toEqual([
      { role: 'user', content: 'keep this', timestamp: '' },
    ])

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'keepalive' }),
    })
    expect(store.hostConnectionUnavailable).toBe(false)
  })

  test('a successful poll clears the host-unreachable banner', async () => {
    // The banner was only cleared from a chat WebSocket frame. If the socket
    // stayed down (or no chat was open) after the host came back, "Can't reach
    // the host" sat on screen over a working connection until a page reload.
    // syncLatest is proxied to the host, so a 200 is proof it is reachable.
    const store = useProjectStore()
    const chatId = 'c-recovers'
    store.activeChatId = chatId
    store.messages[chatId] = []
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'host_unreachable' }),
    })
    expect(store.hostConnectionUnavailable).toBe(true)

    apiGet.mockResolvedValueOnce([])   // /api/chats answered by the host
    await store.syncLatest()

    expect(store.hostConnectionUnavailable).toBe(false)
  })

  test('treats the legacy generic event as the same single connection state', () => {
    const store = useProjectStore()
    const chatId = 'c-client-legacy'
    store.activeChatId = chatId
    store.messages[chatId] = []
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'error',
        message: 'Host WS unreachable: host offline',
      }),
    })

    expect(store.hostConnectionUnavailable).toBe(true)
    expect(store.messages[chatId]).toEqual([])
  })
})

describe('ephemeral status events', () => {
  test('does not render Claude requesting status as a system message', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-requesting'
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'user', content: 'hi', timestamp: '' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'requesting' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'requesting' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'retrying on sonnet' }),
    })

    const msgsFinal = store.messages[chatId] || []
    expect(msgsFinal.some(m => m.role === 'system' && m.content === 'requesting')).toBe(false)
    expect(msgsFinal.some(m => m.role === 'system' && m.content === 'retrying on sonnet')).toBe(true)
  })

  test('does not render allowed or allowed_warning rate limit status as a system message', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-ratelimit'
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'user', content: 'hi', timestamp: '' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'Rate limit: allowed (five_hour)' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'Rate limit: allowed_warning (five_hour) 90.0% used' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'Rate limit: rejected (five_hour)' }),
    })

    const msgs = store.messages[chatId] || []
    expect(msgs.some(m => m.role === 'system' && m.content.includes('Rate limit: allowed (five_hour)'))).toBe(false)
    expect(msgs.some(m => m.role === 'system' && m.content.includes('Rate limit: allowed_warning'))).toBe(false)
    expect(msgs.some(m => m.role === 'system' && m.content.includes('Rate limit: rejected'))).toBe(false)
  })

  test('folds repeated compacting status ticks into one live trace line, not stacked system bubbles', () => {
    // Regression: each "compacting" tick from the CLI used to push its own
    // top-level system bubble (3 identical "compacting" bubbles stacked
    // above the live Thinking trace instead of one line inside it).
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-compacting'
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'user', content: 'hi', timestamp: '' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'compacting' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'compacting' }),
    })
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'status', message: 'compacting' }),
    })

    const msgs = store.messages[chatId] || []
    expect(msgs.some(m => m.content === 'compacting')).toBe(false)

    const statusEntries = store.currentTimeline.filter(e => e.kind === 'status')
    expect(statusEntries.length).toBe(1)
    expect(statusEntries[0].content).toBe('compacting')
  })
})

describe('subagent thinking deltas', () => {
  test('do not leak a subagent thinking delta into the parent turn trace or messages', () => {
    // Regression: thinking deltas fired from inside a Task subagent arrive
    // with parent_tool_use_id set. The PWA already renders the subagent's
    // transcript in its own "Subagent activity" box, so accumulating the
    // delta into the parent's thinking buffer used to produce a stray
    // _thinking message at the end of the parent turn's trace block and
    // persist it into the chat history after the result event.
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-subagent-thinking'
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'user', content: 'run the audit', timestamp: '' },
    ]
    store.connectWs(chatId)
    const socket = fakeSockets[0]

    // Subagent-emitted thinking delta: must be dropped, not appended.
    socket.onmessage?.({
      data: JSON.stringify({
        type: 'thinking',
        text: 'subagent reasoning that should not appear in the parent trace',
        parent_tool_use_id: 'task-1',
      }),
    })
    // Top-level thinking delta on the same chat: must accumulate as before.
    socket.onmessage?.({
      data: JSON.stringify({
        type: 'thinking',
        text: 'parent reasoning stays in the trace',
      }),
    })
    // Mid-stream: the parent's thinking buffer holds only the parent text;
    // the subagent delta was discarded.
    expect(store.streamingThinking[chatId]).toBe('parent reasoning stays in the trace')

    // End-of-turn flush: the parent thinking is persisted as a _thinking
    // system message; the subagent text must not appear anywhere in messages.
    socket.onmessage?.({
      data: JSON.stringify({
        type: 'result',
        text: 'done',
        is_error: false,
        effective_model: 'claude-test',
        usage: {},
        session_id: 'sess-1',
      }),
    })

    const thinkingMsgs = (store.messages[chatId] || []).filter(m => m.tool_name === '_thinking')
    expect(thinkingMsgs.map(m => m.content)).toEqual(['parent reasoning stays in the trace'])
  })
})

describe('pinned file dismissal', () => {
  const surfacedEvent = {
    type: 'tool_use',
    tool_name: 'file_surface',
    tool_use_id: 'surface-1',
    file_touch: {
      file_path: '/workspace/report.md',
      action: 'surfaced',
    },
  }

  const otherSurfacedEvent = {
    ...surfacedEvent,
    tool_use_id: 'surface-2',
    file_touch: {
      file_path: '/workspace/plan.md',
      action: 'surfaced',
    },
  }

  test('keeps a user-closed pinned file closed when chat events replay', () => {
    Object.defineProperty(window, 'innerWidth', {
      value: 1200,
      configurable: true,
    })
    const chatId = 'chat-pinned-dismissal'
    const store = useProjectStore()
    store.activeChatId = chatId
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({ data: JSON.stringify(surfacedEvent) })
    expect(store.pinnedFileFor(chatId)).toBe('/workspace/report.md')

    store.unpinFile(chatId)
    expect(store.pinnedFileFor(chatId)).toBeUndefined()

    fakeSockets[0].onmessage?.({ data: JSON.stringify(surfacedEvent) })
    expect(store.pinnedFileFor(chatId)).toBeUndefined()
  })

  test('surfaces a different file after one was dismissed', () => {
    Object.defineProperty(window, 'innerWidth', {
      value: 1200,
      configurable: true,
    })
    const chatId = 'chat-pinned-next-artifact'
    const store = useProjectStore()
    store.activeChatId = chatId
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({ data: JSON.stringify(surfacedEvent) })
    store.unpinFile(chatId)

    // A new deliverable is not the file the user closed, so it must open.
    fakeSockets[0].onmessage?.({ data: JSON.stringify(otherSurfacedEvent) })
    expect(store.pinnedFileFor(chatId)).toBe('/workspace/plan.md')
  })

  test('an explicit surface replaces whatever is already pinned', () => {
    Object.defineProperty(window, 'innerWidth', {
      value: 1200,
      configurable: true,
    })
    const chatId = 'chat-pinned-replace'
    const store = useProjectStore()
    store.activeChatId = chatId
    store.connectWs(chatId)

    store.pinFile(chatId, '/workspace/report.md')
    fakeSockets[0].onmessage?.({ data: JSON.stringify(otherSurfacedEvent) })
    expect(store.pinnedFileFor(chatId)).toBe('/workspace/plan.md')
  })

  test('persists the dismissal across store recreation until the user pins a file', () => {
    Object.defineProperty(window, 'innerWidth', {
      value: 1200,
      configurable: true,
    })
    const chatId = 'chat-pinned-reopen'
    const firstStore = useProjectStore()
    firstStore.activeChatId = chatId
    firstStore.connectWs(chatId)
    fakeSockets[0].onmessage?.({ data: JSON.stringify(surfacedEvent) })
    firstStore.unpinFile(chatId)

    setActivePinia(createPinia())
    const reopenedStore = useProjectStore()
    reopenedStore.activeChatId = chatId
    reopenedStore.connectWs(chatId)
    fakeSockets[1].onmessage?.({ data: JSON.stringify(surfacedEvent) })
    expect(reopenedStore.pinnedFileFor(chatId)).toBeUndefined()

    reopenedStore.pinFile(chatId, '/workspace/report.md')
    expect(reopenedStore.pinnedFileFor(chatId)).toBe('/workspace/report.md')
  })

  test('drops a legacy chat-wide dismissal so later surfaces still open', () => {
    Object.defineProperty(window, 'innerWidth', {
      value: 1200,
      configurable: true,
    })
    const chatId = 'chat-pinned-legacy'
    // Written before the store is created: restoreState() runs on setup.
    localStorage.setItem('ciao-dismissed-auto-pins', JSON.stringify({ [chatId]: true }))

    setActivePinia(createPinia())
    const store = useProjectStore()
    store.activeChatId = chatId
    store.connectWs(chatId)

    fakeSockets[fakeSockets.length - 1].onmessage?.({ data: JSON.stringify(surfacedEvent) })
    expect(store.pinnedFileFor(chatId)).toBe('/workspace/report.md')
  })
})

describe('chat closing and re-entry orientation', () => {
  test('deletes an unused draft chat instead of leaving it in the sidebar', async () => {
    const store = useProjectStore()
    const chatId = 'chat-unused-draft'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'New Chat',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.messages[chatId] = []
    store.activeChatId = chatId
    apiDel.mockResolvedValue({ ok: true, deleted: true })

    await store.closeChat()

    // only_if_empty makes the server re-apply its own _is_empty_chat rule.
    // The client cannot see user_turn_count, so it must not be the one
    // deciding whether a chat is disposable.
    expect(apiDel).toHaveBeenCalledWith(`/api/chats/${chatId}?only_if_empty=1`)
    expect(store.chats).toHaveLength(0)
    expect(store.activeChatId).toBeNull()
    expect(routerPush).toHaveBeenCalledWith('/')
  })

  test('keeps a chat holding an unsent composer draft', async () => {
    // Esc closes the chat even while the composer is focused, so a chat whose
    // only content is a typed-but-unsent prompt must not be treated as a
    // discardable draft.
    const store = useProjectStore()
    const chatId = 'chat-with-draft'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'New Chat',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.messages[chatId] = []
    store.activeChatId = chatId
    localStorage.setItem('ciao-chat-drafts', JSON.stringify({ [chatId]: 'half a thought' }))

    await store.closeChat()

    expect(apiDel).not.toHaveBeenCalled()
    expect(store.chats).toHaveLength(1)
    localStorage.removeItem('ciao-chat-drafts')
  })

  test('keeps a chat holding only a staged image', async () => {
    // The server cannot see a staged attachment either, so if the client calls
    // the chat empty the delete goes through and the screenshot goes with it.
    const store = useProjectStore()
    const chatId = 'chat-with-image'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'New Chat',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.messages[chatId] = []
    store.activeChatId = chatId
    store.pendingImages = ['img-1']

    await store.closeChat()

    expect(apiDel).not.toHaveBeenCalled()
    expect(store.chats).toHaveLength(1)
    store.pendingImages = []
  })

  test('keeps a chat the server declines to delete', async () => {
    // The client cannot see user_turn_count, so its "is this a draft" guess
    // can be wrong — a chat whose messages are not loaded, or one that just
    // got a fresh session, looks empty locally. The server has the real rule
    // and says no; closing must then be an ordinary close, not a deletion.
    const store = useProjectStore()
    const chatId = 'chat-looks-empty'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'New Chat',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.messages[chatId] = []
    store.activeChatId = chatId
    apiDel.mockResolvedValue({ ok: false, deleted: false, reason: 'not empty' })

    await store.closeChat()

    expect(store.chats).toHaveLength(1)
    expect(store.activeChatId).toBeNull()
    expect(routerPush).toHaveBeenCalledWith('/')
  })

  test('starts the summary when a completed chat closes and reuses it on reopen', async () => {
    const store = useProjectStore()
    const chatId = 'chat-reentry'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'A completed chat',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: 'session-1',
      created_at: '',
      archived: false,
    }]
    store.messages[chatId] = [{ role: 'user', content: 'Continue this later', timestamp: '' }]
    store.activeChatId = chatId
    apiGet.mockResolvedValue([])
    apiPost.mockImplementation((path: string) => {
      if (path.endsWith('/reentry-summary')) return Promise.resolve({ summary: '• Continue the open task' })
      return Promise.resolve({})
    })

    await store.closeChat()
    expect(apiDel).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(store.reentrySummaries[chatId]).toBe('• Continue the open task'))
    const summaryCalls = apiPost.mock.calls.filter(([path]) => path.endsWith('/reentry-summary')).length
    await store.switchChat(chatId)
    await vi.waitFor(() => expect(store.reentrySummaries[chatId]).toBe('• Continue the open task'))
    expect(apiPost.mock.calls.filter(([path]) => path.endsWith('/reentry-summary')).length).toBe(summaryCalls)
  })

  test('requests a summary when selecting any existing chat, without an explicit close', async () => {
    const store = useProjectStore()
    const firstChatId = 'chat-first'
    const secondChatId = 'chat-second'
    store.chats = [firstChatId, secondChatId].map(chat_id => ({
      chat_id,
      project_id: 'p1',
      title: chat_id,
      model: 'sonnet',
      provider: 'claude' as const,
      mode: 'auto',
      session_id: `${chat_id}-session`,
      created_at: '',
      archived: false,
    }))
    apiGet.mockResolvedValue([])
    apiPost.mockImplementation((path: string) => {
      if (path.endsWith('/reentry-summary')) {
        return Promise.resolve({ summary: `• Summary for ${path.includes(firstChatId) ? 'first' : 'second'}` })
      }
      return Promise.resolve({})
    })

    await store.switchChat(firstChatId)
    await vi.waitFor(() => expect(store.reentrySummaries[firstChatId]).toBe('• Summary for first'))

    await store.switchChat(secondChatId)
    await vi.waitFor(() => expect(store.reentrySummaries[secondChatId]).toBe('• Summary for second'))
  })
})

describe('re-entry summary invalidation', () => {
  test('clears the summary when a new user message arrives over the chat socket', () => {
    const store = useProjectStore()
    const chatId = 'chat-summary-user'
    store.reentrySummaries[chatId] = 'Old orientation'
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        text: 'A new prompt',
        turn_index: 4,
      }),
    })

    expect(store.reentrySummaries[chatId]).toBeUndefined()
  })

  test('clears the summary when a new assistant result arrives', () => {
    const store = useProjectStore()
    const chatId = 'chat-summary-result'
    store.reentrySummaries[chatId] = 'Old orientation'
    store.connectWs(chatId)
    // Mark the chat as streaming so the result is treated as a real turn
    // completion rather than a broker replay on WS resume.
    store.streaming[chatId] = true

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'result',
        text: 'The new answer',
        is_error: false,
        effective_model: 'claude-test',
        usage: {},
        session_id: 'session-1',
      }),
    })

    expect(store.reentrySummaries[chatId]).toBeUndefined()
  })

  test('keeps the summary when a result is replayed after the turn has already settled', () => {
    // A WS reconnect replays the broker's buffered events. A result for a
    // turn that already settled on this client must NOT clear the re-entry
    // summary — otherwise scrolling after a resume would dismiss the
    // orientation note.
    const store = useProjectStore()
    const chatId = 'chat-summary-result-replay'
    store.reentrySummaries[chatId] = 'Old orientation'
    store.connectWs(chatId)
    // streaming stays false (default) — the turn already settled.

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'result',
        text: 'The new answer',
        is_error: false,
        effective_model: 'claude-test',
        usage: {},
        session_id: 'session-1',
      }),
    })

    expect(store.reentrySummaries[chatId]).toBe('Old orientation')
  })

  test('keeps the summary when a user_echo is replayed for an already-rendered turn', () => {
    const store = useProjectStore()
    const chatId = 'chat-summary-echo-replay'
    store.reentrySummaries[chatId] = 'Old orientation'
    store.connectWs(chatId)
    // The transcript already has a user message with the same turn_index
    // that the echo is replaying. The summary must survive this echo so
    // that scrolling (which can trigger a WS resume and a buffered echo
    // replay) doesn't dismiss the orientation note.
    store.messages[chatId] = [{
      role: 'user',
      content: 'Earlier prompt',
      timestamp: '2026-08-13T10:00:00Z',
      turn_index: 4,
    }]

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        text: 'Earlier prompt',
        turn_index: 4,
      }),
    })

    expect(store.reentrySummaries[chatId]).toBe('Old orientation')
  })

  test('does not restore a stale summary after a new message arrives', async () => {
    const store = useProjectStore()
    const chatId = 'chat-summary-race'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Summary race',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: 'session-1',
      created_at: '',
      archived: false,
    }]

    let resolveSummary!: (value: { summary: string }) => void
    apiPost.mockReturnValue(new Promise(resolve => { resolveSummary = resolve }))
    const request = store.requestReentrySummary(chatId)

    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        text: 'A newer prompt',
        turn_index: 5,
      }),
    })
    resolveSummary({ summary: 'Stale orientation' })
    await request

    expect(store.reentrySummaries[chatId]).toBeUndefined()
  })

  test('does not request a summary when the user has disabled the preference', () => {
    const store = useProjectStore()
    const chatId = 'chat-summary-disabled'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Disabled',
      model: 'sonnet',
      provider: 'claude',
      mode: 'auto',
      session_id: 'session-1',
      created_at: '',
      archived: false,
    }]
    localStorageData['ciao-reentry-summary-enabled'] = 'false'

    store.requestReentrySummaryIfUseful(chatId)

    const calls = apiPost.mock.calls.filter(([path]) => path.endsWith('/reentry-summary'))
    expect(calls).toHaveLength(0)
  })

  test('disabling the preference evicts any cached summaries', () => {
    const store = useProjectStore()
    store.reentrySummaries['a'] = 'First'
    store.reentrySummaries['b'] = 'Second'

    store.setReentrySummaryEnabled(false)

    expect(store.reentrySummaries).toEqual({})
  })

  test('enabling the preference leaves cached summaries alone', () => {
    const store = useProjectStore()
    store.reentrySummaries['a'] = 'First'

    store.setReentrySummaryEnabled(true)

    expect(store.reentrySummaries['a']).toBe('First')
  })
})

describe('queued message replay handling', () => {
  test('keeps later queued messages visible when the first follow-up starts', () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.queuedMessages[chatId] = [
      { id: 'q-1', text: 'msg A' },
      { id: 'q-2', text: 'msg B' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        entry_id: 'q-1',
        text: 'msg A',
        turn_index: 1,
      }),
    })

    expect(store.queuedMessages[chatId]).toEqual([
      { id: 'q-2', text: 'msg B' },
    ])
  })

  test('legacy echoes remove only one matching queue entry', () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.queuedMessages[chatId] = [
      { id: 'q-1', text: 'same prompt' },
      { id: 'q-2', text: 'same prompt' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        text: 'same prompt',
        turn_index: 1,
      }),
    })

    expect(store.queuedMessages[chatId]).toEqual([
      { id: 'q-2', text: 'same prompt' },
    ])
  })

  test('an echo for the current turn does not clear unrelated queued messages', () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.queuedMessages[chatId] = [
      { id: 'q-1', text: 'follow-up A' },
      { id: 'q-2', text: 'follow-up B' },
    ]
    store.connectWs(chatId)

    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        text: 'original turn',
        turn_index: 0,
      }),
    })

    expect(store.queuedMessages[chatId]).toEqual([
      { id: 'q-1', text: 'follow-up A' },
      { id: 'q-2', text: 'follow-up B' },
    ])
  })

  test('clears local queued chips when server history contains the flushed user turn', async () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.queuedMessages[chatId] = [{ id: 'q-1', text: 'msg A' }]
    apiGet.mockResolvedValue([
      { role: 'user', content: 'initial', sent_at: '', turn_index: 0 },
      { role: 'assistant', content: 'reply', sent_at: '' },
      { role: 'user', content: 'msg A', sent_at: '', turn_index: 1 },
    ])

    await store.loadMessages(chatId)

    expect(store.queuedMessages[chatId]).toBeUndefined()
  })

  test('ignores stale queued replay when the flushed user turn is already hydrated', () => {
    const store = useProjectStore()
    const chatId = 'chat-queue'
    store.messages[chatId] = [
      { role: 'user', content: 'initial', timestamp: '', turn_index: 0 },
      { role: 'assistant', content: 'reply', timestamp: '' },
      { role: 'user', content: 'msg A', timestamp: '', turn_index: 1 },
    ]

    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'queued', id: 'q-1', text: 'msg A' }),
    })

    expect(store.queuedMessages[chatId]).toBeUndefined()
  })
})

describe('optimistic user bubble reconciliation', () => {
  test('reconciles a bubble stranded behind a prior turn instead of duplicating it', async () => {
    // Repro: the user sends while the client thinks it is idle, so an
    // optimistic bubble (no turn_index) is rendered. The server queues the
    // send behind a still-running turn whose activity/assistant blocks stream
    // on top, then records + echoes the turn later with a fresh turn_index.
    // The echo must reconcile the stranded bubble, not push a second copy.
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'chat-strand'
    store.messages[chatId] = [
      { role: 'user', content: 'queued question', timestamp: '', turn_index: undefined },
      { role: 'assistant', content: 'prior turn reply', timestamp: '' },
    ]

    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'user_echo',
        text: 'queued question',
        turn_index: 1,
        sent_at: '2026-07-16T13:07:59Z',
      }),
    })

    const userMsgs = store.messages[chatId].filter(
      m => m.role === 'user' && m.content === 'queued question',
    )
    expect(userMsgs.length).toBe(1)
    expect(userMsgs[0].turn_index).toBe(1)
  })

  test('upgrades an optimistic bubble in place when nothing streamed between send and echo', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'chat-fast'
    store.messages[chatId] = [
      { role: 'assistant', content: 'earlier reply', timestamp: '' },
      { role: 'user', content: 'hello', timestamp: '', turn_index: undefined },
    ]

    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'user_echo', text: 'hello', turn_index: 2 }),
    })

    const userMsgs = store.messages[chatId].filter(
      m => m.role === 'user' && m.content === 'hello',
    )
    expect(userMsgs.length).toBe(1)
    expect(userMsgs[0].turn_index).toBe(2)
  })

  test('loadMessages heals an orphaned optimistic bubble the live echo missed', async () => {
    // Existing chats already carrying the duplicate must self-heal on reload:
    // the server session holds the turn exactly once, so the shorter server
    // history would otherwise be blocked by the never-shrink guard.
    const store = useProjectStore()
    const chatId = 'chat-heal'
    store.messages[chatId] = [
      { role: 'user', content: 'dup question', timestamp: '', turn_index: undefined },
      { role: 'assistant', content: 'prior reply', timestamp: '' },
      { role: 'user', content: 'dup question', timestamp: '2026-07-16T13:07:59Z', turn_index: 1 },
      { role: 'assistant', content: 'answer', timestamp: '' },
    ]
    apiGet.mockResolvedValue([
      { role: 'assistant', content: 'prior reply', sent_at: '' },
      { role: 'user', content: 'dup question', sent_at: '2026-07-16T13:07:59Z', turn_index: 1 },
      { role: 'assistant', content: 'answer', sent_at: '' },
    ])

    await store.loadMessages(chatId)

    const userMsgs = store.messages[chatId].filter(
      m => m.role === 'user' && m.content === 'dup question',
    )
    expect(userMsgs.length).toBe(1)
    expect(userMsgs[0].turn_index).toBe(1)
  })

  test('loadMessages discards trailing optimistic user bubbles when server has settled without them', async () => {
    const store = useProjectStore()
    const chatId = 'chat-unsent'
    store.messages[chatId] = [
      { role: 'user', content: 'prior question', timestamp: '2026-07-16T13:07:59Z', turn_index: 1 },
      { role: 'assistant', content: 'prior reply', timestamp: '' },
      { role: 'user', content: 'unsent question', timestamp: '', turn_index: undefined },
    ]
    apiGet.mockResolvedValue([
      { role: 'user', content: 'prior question', sent_at: '2026-07-16T13:07:59Z', turn_index: 1 },
      { role: 'assistant', content: 'prior reply', sent_at: '' },
    ])

    await store.loadMessages(chatId)

    const msgs = store.messages[chatId]
    expect(msgs.length).toBe(2)
    expect(msgs.some(m => m.content === 'unsent question')).toBe(false)
  })

  test('loadMessages keeps local history to avoid data loss if server has fewer completed user turns', async () => {
    const store = useProjectStore()
    const chatId = 'chat-dataloss'
    store.messages[chatId] = [
      { role: 'user', content: 'question 1', timestamp: '', turn_index: 1 },
      { role: 'assistant', content: 'reply 1', timestamp: '' },
      { role: 'user', content: 'question 2', timestamp: '', turn_index: 2 },
      { role: 'assistant', content: 'reply 2', timestamp: '' },
    ]
    // Server session reset, only has question 2
    apiGet.mockResolvedValue([
      { role: 'user', content: 'question 2', sent_at: '', turn_index: 1 },
      { role: 'assistant', content: 'reply 2', sent_at: '' },
    ])

    await store.loadMessages(chatId)

    const msgs = store.messages[chatId]
    expect(msgs.length).toBe(4) // Keeps local to avoid data loss
  })

  test('loadMessages skips mid-turn assistant progress while the server is streaming', async () => {
    // The server session file is updated in real time during a streaming turn.
    // If /messages returns that partial progress while the live trace owns the
    // current turn, the PWA would render two Activity rows: one historical and
    // one live. Truncate the server response to the last known user message.
    const store = useProjectStore()
    const chatId = 'chat-streaming'
    store.messages[chatId] = [
      { role: 'user', content: 'prior question', timestamp: '', turn_index: 0 },
      { role: 'assistant', content: 'prior reply', timestamp: '' },
      { role: 'user', content: 'check first', timestamp: '', turn_index: 1 },
    ]
    store.projectStreaming[chatId] = true
    apiGet.mockResolvedValue([
      { role: 'user', content: 'prior question', sent_at: '', turn_index: 0 },
      { role: 'assistant', content: 'prior reply', sent_at: '' },
      { role: 'user', content: 'check first', sent_at: '', turn_index: 1 },
      // Mid-turn progress that must not land in the historical timeline yet.
      { role: 'system', content: 'Read file.md', tool_name: '_activity', sent_at: '' },
      { role: 'system', content: 'thinking...', tool_name: '_thinking', sent_at: '' },
    ])

    await store.loadMessages(chatId)

    const msgs = store.messages[chatId]
    expect(msgs.length).toBe(3)
    expect(msgs.some(m => m.tool_name === '_activity')).toBe(false)
    expect(msgs.some(m => m.tool_name === '_thinking')).toBe(false)
  })
})

describe('Codex structured questions', () => {
  test('answers a native request inside the active websocket turn', () => {
    const store = useProjectStore()
    const chatId = 'codex-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Codex',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'auto',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]
    socket.onmessage?.({
      data: JSON.stringify({
        type: 'tool_use',
        tool_name: 'AskUserQuestion',
        request_id: 'codex-1',
        tool_input: JSON.stringify({
          questions: [{
            id: 'choice',
            header: 'Choice',
            question: 'Pick one',
            isOther: false,
            isSecret: false,
            options: [{ label: 'A', description: 'first' }],
          }],
        }),
      }),
    })

    expect(store.activeQuestions[chatId][0]).toMatchObject({
      id: 'choice',
      requestId: 'codex-1',
      allowOther: false,
      question: 'Pick one',
    })

    store.respondQuestion(chatId, 'codex-1', { choice: ['A'] })

    expect(store.activeQuestions[chatId]).toBeUndefined()
    expect(socket.send).toHaveBeenCalledWith(JSON.stringify({
      type: 'question_response',
      request_id: 'codex-1',
      answers: { choice: ['A'] },
    }))
  })

  test('does not resurrect an answered picker from a stale server snapshot', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'codex-stale'
    // The persisted payload carries the request id, exactly as the backend
    // embeds it into pending_question for native providers.
    const pending = JSON.stringify({
      request_id: 'codex-1',
      questions: [{
        id: 'choice',
        header: 'Choice',
        question: 'Pick one',
        isOther: false,
        options: [{ label: 'A', description: 'first' }],
      }],
    })
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Codex',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'auto',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
      pending_question: pending,
    }]
    store.activeChatId = chatId
    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'tool_use',
        tool_name: 'AskUserQuestion',
        request_id: 'codex-1',
        tool_input: JSON.stringify({
          questions: [{
            id: 'choice',
            header: 'Choice',
            question: 'Pick one',
            isOther: false,
            options: [{ label: 'A', description: 'first' }],
          }],
        }),
      }),
    })
    expect(store.activeQuestions[chatId]).toHaveLength(1)

    store.respondQuestion(chatId, 'codex-1', { choice: ['A'] })
    expect(store.activeQuestions[chatId]).toBeUndefined()

    // A poll/reconnect races the server clear: the snapshot still carries the
    // now-answered pending_question. loadMessages runs rebuildPendingQuestion,
    // which must refuse to bring the picker back.
    store.chats[0].pending_question = pending
    await store.loadMessages(chatId)
    expect(store.activeQuestions[chatId]).toBeUndefined()
  })

  test('rebuilds a genuinely new question after an earlier one was answered', async () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'codex-next'
    const mkPayload = (rid: string) => JSON.stringify({
      request_id: rid,
      questions: [{ id: 'choice', header: 'Choice', question: 'Pick one', options: [{ label: 'A' }] }],
    })
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Codex',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'auto',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
    }]
    store.activeChatId = chatId
    store.connectWs(chatId)
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({
        type: 'tool_use',
        tool_name: 'AskUserQuestion',
        request_id: 'codex-1',
        tool_input: mkPayload('codex-1'),
      }),
    })
    store.respondQuestion(chatId, 'codex-1', { choice: ['A'] })
    expect(store.activeQuestions[chatId]).toBeUndefined()

    // A distinct later question (new request id) must still surface on rebuild.
    store.chats[0].pending_question = mkPayload('codex-2')
    await store.loadMessages(chatId)
    expect(store.activeQuestions[chatId]?.[0]).toMatchObject({ requestId: 'codex-2' })
  })

  test('chatNeedsInput reflects live and persisted AskUserQuestion state', () => {
    const store = useProjectStore()
    const chatId = 'question-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Question',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'auto',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
      pending_question: JSON.stringify({
        questions: [{ id: 'q1', question: 'Pick one', options: [{ label: 'A' }] }],
      }),
    }]

    expect(store.chatNeedsInput(chatId)).toBe(true)

    store.activeQuestions[chatId] = [{
      id: 'q1',
      question: 'Pick one',
      header: '',
      multiSelect: false,
      allowOther: false,
      isSecret: false,
      requestId: 'req-1',
      options: [{ label: 'A', description: '' }],
    }]
    expect(store.chatNeedsInput(chatId)).toBe(true)

    delete store.activeQuestions[chatId]
    store.chats[0].pending_question = ''
    expect(store.chatNeedsInput(chatId)).toBe(false)
  })

  test('parses alternate text/type AskUserQuestion payloads', () => {
    // MiniMax (and possibly other Claude-compatible providers) emit
    // `text` + `type: single_select` instead of `question`/`multiSelect`.
    const store = useProjectStore()
    const chatId = 'alt-schema-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Alt',
      model: 'minimax-m3:cloud',
      provider: 'claude',
      mode: 'auto',
      session_id: 's1',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[fakeSockets.length - 1]
    socket.onmessage?.({
      data: JSON.stringify({
        type: 'tool_use',
        tool_name: 'AskUserQuestion',
        tool_input: JSON.stringify({
          questions: [
            {
              text: 'How do you want to handle the booking form?',
              type: 'single_select',
              options: [
                { label: 'A. Link manually', value: 'manual' },
                { label: 'B. Leave as-is', value: 'skip' },
              ],
            },
            {
              text: 'Which guests first?',
              type: 'multi_select',
              options: [{ label: 'All Yes', value: 'all_yes' }],
            },
          ],
        }),
      }),
    })

    expect(store.activeQuestions[chatId]).toHaveLength(2)
    expect(store.activeQuestions[chatId][0]).toMatchObject({
      question: 'How do you want to handle the booking form?',
      multiSelect: false,
    })
    expect(store.activeQuestions[chatId][0].options.map(o => o.label)).toEqual([
      'A. Link manually',
      'B. Leave as-is',
    ])
    expect(store.activeQuestions[chatId][1]).toMatchObject({
      question: 'Which guests first?',
      multiSelect: true,
    })
  })

  test('surfaces an empty AskUserQuestion as a free-form fallback', () => {
    const store = useProjectStore()
    const chatId = 'empty-question-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Empty question',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'auto',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[fakeSockets.length - 1]
    socket.onmessage?.({
      data: JSON.stringify({
        type: 'tool_use',
        tool_name: 'AskUserQuestion',
        request_id: 'codex-empty-1',
        tool_input: JSON.stringify({ questions: [] }),
      }),
    })

    expect(store.activeQuestions[chatId]).toHaveLength(1)
    expect(store.activeQuestions[chatId][0]).toMatchObject({
      id: '__freeform__',
      allowOther: true,
      requestId: 'codex-empty-1',
    })
    expect(store.activeQuestions[chatId][0].question).toContain('needs your input')
  })

  test('surfaces approval requests and preserves Codex quota metadata', () => {
    const store = useProjectStore()
    const chatId = 'codex-gates'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Codex',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'normal',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]
    socket.onmessage?.({ data: JSON.stringify({
      type: 'permission_request',
      request_id: 'approval-1',
      tool_name: 'Bash',
      tool_input: 'touch safe.txt',
      message: 'Approve?',
    }) })

    expect(store.pendingPermissions[chatId][0].request_id).toBe('approval-1')
    store.respondPermission(chatId, 'approval-1', true)
    expect(socket.send).toHaveBeenCalledWith(JSON.stringify({
      type: 'permission_response',
      request_id: 'approval-1',
      approved: true,
      reason: '',
    }))

    socket.onmessage?.({ data: JSON.stringify({
      type: 'result',
      text: 'done',
      is_error: false,
      effective_model: 'gpt-test',
      usage: { input_tokens: '10' },
      quota: { planType: 'plus', utilization: '0.2' },
      session_id: 'thread-1',
    }) })
    expect(store.messages[chatId].at(-1)?.quota).toEqual({
      planType: 'plus',
      utilization: '0.2',
    })
  })
})

describe('image-capability questions', () => {
  test('parses a model_capability_question event into the active list', () => {
    const store = useProjectStore()
    const chatId = 'cap-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Cap',
      model: 'deepseek-v4-flash:cloud',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]
    socket.onmessage?.({ data: JSON.stringify({
      type: 'model_capability_question',
      request_id: 'cap-abc',
      missing: 'image_input',
      current_model: 'deepseek-v4-flash:cloud',
      candidates: [
        { id: 'deepseek-v4-flash:cloud', label: 'deepseek-v4-flash:cloud', disabled: true },
        { id: 'minimax-m3:cloud', label: 'minimax-m3:cloud', supports_vision: true },
      ],
      timeout_s: 30,
    }) })

    const q = store.activeCapabilityQuestions[chatId]?.[0]
    expect(q).toMatchObject({
      request_id: 'cap-abc',
      missing: 'image_input',
      current_model: 'deepseek-v4-flash:cloud',
      timeout_s: 30,
    })
    expect(q?.candidates[0]).toMatchObject({ id: 'deepseek-v4-flash:cloud', disabled: true })
    expect(q?.candidates[1]).toMatchObject({ id: 'minimax-m3:cloud', supports_vision: true })
    expect(typeof q?.opened_at).toBe('number')
  })

  test('dedupes a replayed question by request_id', () => {
    const store = useProjectStore()
    const chatId = 'cap-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Cap',
      model: 'deepseek-v4-flash:cloud',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]
    const payload = JSON.stringify({
      type: 'model_capability_question',
      request_id: 'cap-abc',
      missing: 'image_input',
      current_model: 'deepseek-v4-flash:cloud',
      candidates: [],
      timeout_s: 30,
    })
    socket.onmessage?.({ data: payload })
    socket.onmessage?.({ data: payload })
    expect(store.activeCapabilityQuestions[chatId]).toHaveLength(1)
  })

  test('respondCapability sends capability_response and pops the card', () => {
    const store = useProjectStore()
    const chatId = 'cap-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Cap',
      model: 'deepseek-v4-flash:cloud',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]
    socket.onmessage?.({ data: JSON.stringify({
      type: 'model_capability_question',
      request_id: 'cap-abc',
      missing: 'image_input',
      current_model: 'deepseek-v4-flash:cloud',
      candidates: [{ id: 'minimax-m3:cloud', label: 'minimax-m3:cloud', supports_vision: true }],
      timeout_s: 30,
    }) })
    expect(store.activeCapabilityQuestions[chatId]).toHaveLength(1)

    store.respondCapability(chatId, 'cap-abc', 'switch', 'minimax-m3:cloud')

    expect(store.activeCapabilityQuestions[chatId]).toBeUndefined()
    expect(socket.send).toHaveBeenCalledWith(JSON.stringify({
      type: 'capability_response',
      request_id: 'cap-abc',
      action: 'switch',
      model_id: 'minimax-m3:cloud',
    }))
  })

  test('sendMessage clears any open capability question for the chat', () => {
    const store = useProjectStore()
    const chatId = 'cap-chat'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Cap',
      model: 'deepseek-v4-flash:cloud',
      provider: 'claude',
      mode: 'auto',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]
    socket.onmessage?.({ data: JSON.stringify({
      type: 'model_capability_question',
      request_id: 'cap-abc',
      missing: 'image_input',
      current_model: 'deepseek-v4-flash:cloud',
      candidates: [],
      timeout_s: 30,
    }) })
    expect(store.activeCapabilityQuestions[chatId]).toHaveLength(1)

    store.sendMessage(chatId, 'hello')

    expect(store.activeCapabilityQuestions[chatId]).toBeUndefined()
  })
})

describe('Codex assistant message phases', () => {
  test('keeps commentary in the trace and the final answer separate', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'codex-phases'
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'Codex phases',
      model: 'gpt-test',
      provider: 'codex',
      mode: 'normal',
      session_id: 'thread-1',
      created_at: '',
      archived: false,
    }]
    store.connectWs(chatId)
    const socket = fakeSockets[0]

    socket.onmessage?.({ data: JSON.stringify({
      type: 'text_delta',
      text: "I'll check that now.",
      phase: 'commentary',
    }) })
    socket.onmessage?.({ data: JSON.stringify({
      type: 'text_delta',
      text: 'Done.',
      phase: 'final_answer',
    }) })
    socket.onmessage?.({ data: JSON.stringify({
      type: 'result',
      text: 'Done.',
      is_error: false,
      effective_model: 'gpt-test',
      usage: {},
      session_id: 'thread-1',
    }) })

    expect(store.messages[chatId].map(message => ({
      content: message.content,
      phase: message.phase,
    }))).toEqual([
      { content: "I'll check that now.", phase: 'commentary' },
      { content: 'Done.', phase: 'final_answer' },
    ])
  })

  test('renders a commentary-only completed turn as its fallback final', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'codex-commentary-fallback'
    store.connectWs(chatId)
    const socket = fakeSockets[0]

    socket.onmessage?.({ data: JSON.stringify({
      type: 'text_delta',
      text: 'The checks completed successfully.',
      phase: 'commentary',
    }) })
    socket.onmessage?.({ data: JSON.stringify({
      type: 'result',
      text: 'The checks completed successfully.',
      fallback_final: true,
      is_error: false,
      effective_model: 'gpt-test',
      usage: {},
      session_id: 'thread-fallback',
    }) })

    expect(store.messages[chatId].map(message => ({
      content: message.content,
      phase: message.phase,
    }))).toEqual([{
      content: 'The checks completed successfully.',
      phase: 'final_answer',
    }])
    expect(store.streaming[chatId]).toBe(false)
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

  test('keeps Working live when /messages hydrates mid-turn progress text', async () => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    const store = useProjectStore()
    const chatId = 'c-midturn'
    store.chats = [
      { chat_id: chatId, project_id: 'p1', title: 'Mid turn', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeChatId = chatId
    store.messages[chatId] = [
      { role: 'user', content: 'yes make it more robust', timestamp: '', turn_index: 0 },
    ]
    store.streaming[chatId] = true
    store.streamingText[chatId] = 'I\'m in the ciao repo'
    // Server still running this turn — events WS truth.
    store.projectStreaming[chatId] = true

    apiGet.mockImplementation((path: string) => {
      if (path === '/api/chats') {
        return Promise.resolve([
          { chat_id: chatId, project_id: 'p1', title: 'Mid turn', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
        ])
      }
      if (path === `/api/chats/${chatId}/messages`) {
        // Claude session files already contain progress notes mid-turn.
        return Promise.resolve([
          { role: 'user', content: 'yes make it more robust', sent_at: '2026-07-18T08:00:00Z', turn_index: 0 },
          { role: 'assistant', content: 'Interesting — mismatch found.' },
          { role: 'assistant', content: 'I\'m in the ciao repo, not ciaobot. Let me cd:' },
        ])
      }
      if (path === `/api/chats/${chatId}/subagents`) return Promise.resolve([])
      return Promise.resolve([])
    })

    await store.syncLatest()

    expect(store.streaming[chatId]).toBe(true)
    expect(store.projectStreaming[chatId]).toBe(true)
    expect(store.isStreaming).toBe(true)
    expect(store.streamingText[chatId]).toBe('I\'m in the ciao repo')
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

  test('does not set toast or unread marker on background agent completion', () => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const chatId = 'c-nudge'
    store.activeChatId = 'some-other-chat'
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    const fire = (remaining: number, nudged?: boolean) =>
      sock.onmessage?.({
        data: JSON.stringify({ type: 'chat_subagents_ready', chat_id: chatId, project_id: 'p1', remaining, nudged }),
      })

    fire(1)
    expect(store.backgroundAgents[chatId]).toBe(1)

    fire(0, true)
    expect(store.toasts).toHaveLength(0)
    expect(store.unread[chatId]).toBeUndefined()

    store.backgroundAgents[chatId] = 1

    fire(0, false)
    expect(store.toasts).toHaveLength(0)
    expect(store.unread[chatId]).toBeUndefined()
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

describe('postprocessingChats (home tidying list)', () => {
  test('lists only chats whose pipeline is still running, newest archive first', () => {
    const store = useProjectStore()
    store.chats = [
      { chat_id: 'c-done', project_id: 'p1', title: 'Settled', archived: true, postprocess: { state: 'done', step: 'insights', steps: {} } },
      { chat_id: 'c-running', project_id: 'p1', title: 'Running', archived: true, last_activity_at: '2026-08-15T10:00:00Z', postprocess: { state: 'running', step: 'insights', expected: [], steps: {} } },
      { chat_id: 'c-fresher', project_id: 'p1', title: 'Fresher', archived: true, last_activity_at: '2026-08-16T10:00:00Z', postprocess: { state: 'running', step: 'memory_proposals', expected: [], steps: {} } },
      { chat_id: 'c-plain', project_id: 'p1', title: 'No pipeline', archived: true },
    ] as unknown as typeof store.chats
    expect(store.postprocessingChats().map(c => c.chat_id)).toEqual(['c-fresher', 'c-running'])
  })
})

describe('chat_streaming_done clears stale streaming for inactive chats', () => {
  test('an inactive chat finishing clears its orphaned local streaming flag', () => {
    apiGet.mockResolvedValue([])
    const store = useProjectStore()
    const activeId = 'c-active'
    const otherId = 'c-other'
    store.activeChatId = activeId
    // The user was streaming `otherId`, then switched away: its per-chat WS is
    // gone but the local optimistic flag is frozen true, and projectStreaming
    // still reflects the running turn.
    store.streaming[otherId] = true
    store.projectStreaming[otherId] = true
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({
        type: 'chat_streaming_done',
        chat_id: otherId,
        project_id: 'p1',
        is_error: false,
      }),
    })
    // Both flags cleared → the sidebar dot (projectStreaming || streaming)
    // stops showing "working" without a full reload.
    expect(store.projectStreaming[otherId]).toBeUndefined()
    expect(store.streaming[otherId]).toBe(false)
    expect(store.isChatStreaming(otherId)).toBe(false)
  })
})

describe('server restart overlay', () => {
  test('server_restarting over /ws/events flips the global overlay', () => {
    const store = useProjectStore()
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({
        type: 'server_restarting',
        message: 'Ciaobot is waiting for active chats to finish before restarting',
      }),
    })
    expect(store.serverRestarting).toBe(true)
    expect(store.serverRestartMessage).toContain('waiting for active chats')
    expect(reloadWhenServerReady).toHaveBeenCalled()
  })

  test('snapshot.restarting flips the overlay for late connectors', () => {
    const store = useProjectStore()
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({
        type: 'snapshot',
        active_streams: [],
        restarting: true,
      }),
    })
    expect(store.serverRestarting).toBe(true)
  })

  test('per-chat server_restarting undoes the optimistic send and skips the error bubble', () => {
    const store = useProjectStore()
    const chatId = 'c-restart'
    store.messages[chatId] = [
      { role: 'user', content: 'hello', timestamp: '2026-07-17T19:36:00Z' },
    ]
    store.streaming[chatId] = true
    store.connectWs(chatId)
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({
        type: 'server_restarting',
        message: 'Ciaobot is waiting for active chats to finish before restarting',
      }),
    })
    expect(store.serverRestarting).toBe(true)
    expect(store.messages[chatId]).toEqual([])
    expect(store.streaming[chatId]).toBe(false)
  })

  test('legacy error drain message also opens the overlay without an error bubble', () => {
    const store = useProjectStore()
    const chatId = 'c-legacy'
    store.messages[chatId] = [
      { role: 'user', content: 'hello', timestamp: '2026-07-17T19:36:00Z' },
    ]
    store.streaming[chatId] = true
    store.connectWs(chatId)
    const sock = fakeSockets[fakeSockets.length - 1]
    sock.onmessage?.({
      data: JSON.stringify({
        type: 'error',
        message: 'Ciaobot is waiting for active chats to finish before restarting',
      }),
    })
    expect(store.serverRestarting).toBe(true)
    expect(store.messages[chatId]).toEqual([])
  })
})

describe('deep-link chat navigation', () => {
  beforeEach(() => {
    routerPush.mockReset()
    apiPost.mockReset()
    apiGet.mockResolvedValue([])
  })

  test('openChatFromDeepLink switches workspace before opening the chat', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p-personal', name: 'Proj Personal', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p-work', name: 'Proj Work', workspace: 'work', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c-personal', project_id: 'p-personal', title: 'Chat Personal', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'c-work', project_id: 'p-work', title: 'Chat Work', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeWorkspace = 'personal'
    store.activeChatId = 'c-personal'

    await store.openChatFromDeepLink('c-work')

    expect(store.activeWorkspace).toBe('work')
    expect(store.activeChatId).toBe('c-work')
    expect(routerPush).toHaveBeenCalledWith('/chat/c-work')
  })

  test('open_chat event over /ws/events navigates to the target chat', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Proj', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c1', project_id: 'p1', title: 'Chat 1', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'c2', project_id: 'p1', title: 'Chat 2', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeChatId = 'c1'
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]

    sock.onmessage?.({
      data: JSON.stringify({ type: 'open_chat', chat_id: 'c2' }),
    })
    await vi.waitFor(() => {
      expect(store.activeChatId).toBe('c2')
    })
    expect(routerPush).toHaveBeenCalledWith('/chat/c2')
  })

  test('chat_archived event over /ws/events marks chat archived and clears active chat', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Proj', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c1', project_id: 'p1', title: 'Chat 1', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeChatId = 'c1'
    store.connectEventsWs()
    const sock = fakeSockets[fakeSockets.length - 1]

    sock.onmessage?.({
      data: JSON.stringify({ type: 'chat_archived', chat_id: 'c1', project_id: 'p1', archive_path: 'archive/c1.md' }),
    })

    expect(store.chats[0].archived).toBe(true)
    expect(store.chats[0].archive_path).toBe('archive/c1.md')
    expect(store.activeChatId).toBeNull()
  })

  function supervisorWithTwoSubchats(): ChatInfo[] {
    return [
      { chat_id: 'parent', project_id: 'p1', title: 'Parent', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'child-a', project_id: 'p1', title: 'Subchat A', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false, spawned_from_chat_id: 'parent' },
      { chat_id: 'child-b', project_id: 'p1', title: 'Subchat B', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false, spawned_from_chat_id: 'parent' },
    ]
  }

  test('archiving a supervisor marks the subchats the server confirms', async () => {
    const store = useProjectStore()
    store.chats = supervisorWithTwoSubchats()
    apiPost.mockResolvedValue({
      ok: true,
      archived_chat_ids: ['parent', 'child-a', 'child-b'],
    })

    await store.archiveChat('parent')

    expect(apiPost).toHaveBeenCalledWith('/api/chats/parent/archive')
    expect(store.chats.every(chat => chat.archived)).toBe(true)
  })

  test('a subchat the server did not archive stays active and keeps its socket', async () => {
    const store = useProjectStore()
    store.chats = supervisorWithTwoSubchats()
    store.connectWs('child-b')
    const childSocket = fakeSockets[fakeSockets.length - 1]
    // The server archived the parent and one child; child-b failed and is
    // still streaming. Marking it archived anyway would drop it out of the
    // sidebar, recentChats and activeChatsAll while it burns tokens unseen.
    apiPost.mockResolvedValue({
      ok: true,
      archived_chat_ids: ['parent', 'child-a'],
      failed_chat_ids: ['child-b'],
    })

    await store.archiveChat('parent')

    const byId = Object.fromEntries(store.chats.map(c => [c.chat_id, c.archived]))
    expect(byId).toEqual({ parent: true, 'child-a': true, 'child-b': false })
    // Its socket was closed before the POST, so it has to be reopened or the
    // live chat would receive no more tokens or permission prompts.
    expect(childSocket.readyState).toBe(FakeWebSocket.CLOSED)
    expect(fakeSockets.some(s => s !== childSocket && s.url.includes('child-b'))).toBe(true)
    // The failure is reported rather than silently dropped.
    const errors = store.toasts.filter(t => t.variant === 'error')
    expect(errors).toHaveLength(1)
    expect(errors[0].title).toBe('Some subchats were not archived')
  })

  test('a stopped-mid-turn subchat is reported to the user', async () => {
    const store = useProjectStore()
    store.chats = supervisorWithTwoSubchats()
    apiPost.mockResolvedValue({
      ok: true,
      archived_chat_ids: ['parent', 'child-a', 'child-b'],
      stopped_chat_ids: ['child-a'],
    })

    await store.archiveChat('parent')

    const toast = store.toasts.find(t => t.title.includes('mid-turn'))
    expect(toast?.title).toBe('Stopped 1 subchat mid-turn')
    expect(toast?.body).toContain('is not in the archive')
  })

  test('a failed archive POST reconnects the sockets and raises an error toast', async () => {
    const store = useProjectStore()
    store.chats = supervisorWithTwoSubchats()
    store.activeChatId = 'parent'
    store.connectWs('parent')
    store.connectWs('child-a')
    const opened = fakeSockets.slice()
    apiPost.mockRejectedValue(new Error('archive exploded'))

    await expect(store.archiveChat('parent')).rejects.toThrow('archive exploded')

    // Nothing was archived, so nothing may be marked archived.
    expect(store.chats.some(chat => chat.archived)).toBe(false)
    // disconnectWs marked both closes intentional, so onclose scheduled no
    // reconnect; without an explicit one these live chats go permanently
    // silent — no tokens, no permission cards, no AskUserQuestion prompts.
    for (const chatId of ['parent', 'child-a']) {
      expect(fakeSockets.some(s => !opened.includes(s) && s.url.includes(chatId))).toBe(true)
    }
    const errors = store.toasts.filter(t => t.variant === 'error')
    expect(errors).toHaveLength(1)
    expect(errors[0].title).toBe('Could not archive chat')
    // The chat stays open, because it was not archived.
    expect(store.activeChatId).toBe('parent')
  })
})

describe('chat creation', () => {
  test('does not duplicate a chat when its event arrives before the POST response', async () => {
    const store = useProjectStore()
    const chat: ChatInfo = {
      chat_id: 'c-new',
      project_id: 'p1',
      title: 'New Chat',
      model: 'sonnet',
      provider: 'claude',
      mode: 'default',
      session_id: '',
      created_at: '2026-08-02T10:00:00Z',
      archived: false,
    }
    let resolvePost!: (value: ChatInfo) => void
    apiPost.mockReturnValue(new Promise<ChatInfo>(resolve => { resolvePost = resolve }))

    const creating = store.createChat('p1')
    store.connectEventsWs()
    fakeSockets[0].onmessage?.({
      data: JSON.stringify({ type: 'chat_created', chat }),
    })
    resolvePost(chat)
    await creating

    expect(store.chats.filter(c => c.chat_id === chat.chat_id)).toHaveLength(1)
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
    expect(store.bootstrapped).toBe(false)
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/workspaces') {
        return Promise.resolve({
          workspaces: [
            { name: 'home', vault_root: 'memory-vault/home', default_provider: 'opencode', default_model: '', gws_profile: 'personal' },
            { name: 'client', vault_root: 'vaults/client', default_provider: 'claude', default_model: '', gws_profile: 'work' },
          ],
          active: 'home',
          provider_options: [
            { value: 'claude', label: 'Claude' },
            { value: 'opencode', label: 'opencode' },
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
    expect(store.activeChatId).toBeNull()
    expect(store.bootstrapped).toBe(true)
  })

  test('fetchAll starts on home instead of restoring the last open chat', async () => {
    window.localStorage.setItem('ciao-active-chat', 'c-saved')
    const store = useProjectStore()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/workspaces') {
        return Promise.resolve({ workspaces: [], active: 'home', provider_options: [] })
      }
      if (path === '/api/projects') {
        return Promise.resolve([
          { project_id: 'p1', name: 'General', workspace: 'home', context: '', created_at: '', order: 0, vault_folder: 'general' },
        ])
      }
      if (path === '/api/chats') {
        return Promise.resolve([
          { chat_id: 'c-saved', project_id: 'p1', title: 'Last open', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
        ])
      }
      return Promise.resolve([])
    })

    await store.fetchAll()

    expect(store.activeChatId).toBeNull()
    expect(window.localStorage.getItem('ciao-active-chat')).toBeNull()
    expect(apiGet).not.toHaveBeenCalledWith('/api/chats/c-saved/messages')
  })

  // The app shell renders once fetchAll resolves, so it must not wait on the
  // active chat's message history — a long transcript would otherwise delay the
  // whole home page behind parsing one chat.
  test('fetchAll resolves without waiting for the active chat history', async () => {
    window.history.replaceState({}, '', '/chat/c1')
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    const store = useProjectStore()
    let releaseMessages: (value: unknown) => void = () => {}
    const messagesRequested = new Promise<void>(seen => {
      apiGet.mockImplementation((path: string) => {
        if (path === '/api/workspaces') {
          return Promise.resolve({ workspaces: [], active: 'home', provider_options: [] })
        }
        if (path === '/api/projects') {
          return Promise.resolve([
            { project_id: 'p1', name: 'General', workspace: 'home', context: '', created_at: '', order: 0, vault_folder: 'general' },
          ])
        }
        if (path === '/api/chats') {
          return Promise.resolve([
            { chat_id: 'c1', project_id: 'p1', title: 'Long chat', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
          ])
        }
        if (path === '/api/chats/c1/messages') {
          seen()
          // Never resolves until we say so: stands in for a slow transcript.
          return new Promise(resolve => { releaseMessages = resolve })
        }
        return Promise.resolve([])
      })
    })

    await store.fetchAll()

    // Shell is ready even though the history is still in flight.
    expect(store.bootstrapped).toBe(true)
    expect(store.activeChatId).toBe('c1')
    await messagesRequested
    expect(store.messages['c1'] ?? []).toEqual([])

    releaseMessages([])
    window.history.replaceState({}, '', '/')
  })

  // A fetchAll on an explicit chat route must only mark that chat read when it
  // is actually visible. This protects unread state for background tabs while
  // keeping direct chat links read as soon as they are displayed.
  test.each([
    ['visible', true],
    ['hidden', false],
  ])('fetchAll marks the active chat read only when %s', async (visibility, shouldMark) => {
    window.history.replaceState({}, '', '/chat/c1')
    Object.defineProperty(document, 'visibilityState', { value: visibility, configurable: true })
    const store = useProjectStore()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/workspaces') {
        return Promise.resolve({ workspaces: [], active: 'home', provider_options: [] })
      }
      if (path === '/api/projects') {
        return Promise.resolve([
          { project_id: 'p1', name: 'General', workspace: 'home', context: '', created_at: '', order: 0, vault_folder: 'general' },
        ])
      }
      if (path === '/api/chats') {
        return Promise.resolve([
          { chat_id: 'c1', project_id: 'p1', title: 'Done', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
        ])
      }
      return Promise.resolve([])
    })

    await store.fetchAll()

    expect(store.activeChatId).toBe('c1')
    const markedRead = apiPost.mock.calls.some(([path]) => path === '/api/chats/c1/read')
    expect(markedRead).toBe(shouldMark)
    window.history.replaceState({}, '', '/')
  })

  test('restoreState loads the saved selection before fetchAll resolves the launch route', () => {
    window.localStorage.setItem('ciao-active-chat', 'saved-chat')
    const store = useProjectStore()
    expect(store.activeChatId).toBe('saved-chat')
    expect(store.bootstrapped).toBe(false)
  })

  test('switchWorkspace lands on home without loading an arbitrary chat', async () => {
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

    apiGet.mockImplementation((path: string) => {
      if (path === '/api/chats/c-personal/messages') {
        return new Promise(() => {}) // a transcript that would block forever
      }
      return Promise.resolve([])
    })

    await store.switchWorkspace('personal')

    expect(store.activeWorkspace).toBe('personal')
    expect(store.activeChatId).toBeNull()
    expect(routerPush).toHaveBeenCalledWith('/')
    expect(apiGet).not.toHaveBeenCalledWith('/api/chats/c-personal/messages')
    expect(apiPost).not.toHaveBeenCalledWith('/api/chats/c-personal/read', {})
  })

  test('cross-workspace new chat does not wait for the target workspace history', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p-personal', name: 'General', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p-work', name: 'General', workspace: 'work', context: '', created_at: '', order: 0, vault_folder: '' },
    ]
    store.chats = [
      { chat_id: 'c-personal-old', project_id: 'p-personal', title: 'Long chat', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
      { chat_id: 'c-work', project_id: 'p-work', title: 'Work chat', model: '', provider: 'claude', mode: '', session_id: '', created_at: '', archived: false },
    ]
    store.activeWorkspace = 'work'
    store.activeChatId = 'c-work'
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/chats/c-personal-old/messages') {
        return new Promise(() => {})
      }
      return Promise.resolve([])
    })
    apiPost.mockImplementation((path: string) => {
      if (path === '/api/projects/p-personal/chats') {
        return Promise.resolve({
          chat_id: 'c-personal-new',
          project_id: 'p-personal',
          title: 'New Chat',
          model: '',
          provider: 'claude',
          mode: '',
          session_id: '',
          created_at: '',
          archived: false,
        })
      }
      return Promise.resolve({})
    })

    await store.switchWorkspace('personal')
    await store.createChat('p-personal')
    await vi.waitFor(() => expect(store.activeChatId).toBe('c-personal-new'))

    expect(apiPost).toHaveBeenCalledWith('/api/projects/p-personal/chats', { title: 'New Chat' })
    expect(apiGet).not.toHaveBeenCalledWith('/api/chats/c-personal-old/messages')
  })

  test('switchWorkspace with transition false updates workspace and chat ID but does not redirect', async () => {
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

    routerPush.mockClear()
    apiPost.mockClear()

    await store.switchWorkspace('personal', { transition: false })

    expect(store.activeWorkspace).toBe('personal')
    expect(store.activeChatId).toBe('c-personal')
    expect(routerPush).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
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

    // The fix prompt (with the error log + approval-gated GitHub-issue fallback) was sent over the WS.
    const sent = fakeSockets.flatMap(s => (s.send as Mock).mock.calls.map((c: unknown[]) => String(c[0])))
    const fixMsg = sent.find(m => m.includes('Error: boom'))
    expect(fixMsg).toBeTruthy()
    expect(fixMsg).toContain('ask for my approval')
    expect(fixMsg).toContain('gh auth login')
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

describe('conversation forks', () => {
  test('creates a fork with the selected history and switches to it', async () => {
    const store = useProjectStore()
    const sourceId = 'chat-source'
    const copied = [
      { role: 'user' as const, content: 'Question', timestamp: '', turn_index: 0 },
      { role: 'assistant' as const, content: 'Answer', timestamp: '' },
    ]
    store.chats = [{
      chat_id: sourceId,
      project_id: 'project-1',
      title: 'Original',
      model: 'opus',
      provider: 'claude',
      mode: 'auto',
      session_id: 'session-source',
      created_at: '',
      archived: false,
    }]
    store.messages[sourceId] = [...copied, {
      role: 'user',
      content: 'Later question',
      timestamp: '',
      turn_index: 1,
    }]
    store.activeChatId = sourceId
    apiGet.mockResolvedValue([])
    apiPost.mockImplementation((path: string) => {
      if (path === `/api/chats/${sourceId}/fork`) {
        return Promise.resolve({
          ...store.chats[0],
          chat_id: 'chat-fork',
          title: 'Original · Fork 1',
          session_id: '',
        })
      }
      return Promise.resolve({})
    })

    const fork = await store.forkChat(sourceId, copied, 0)

    expect(apiPost).toHaveBeenCalledWith(`/api/chats/${sourceId}/fork`, {
      messages: copied,
      turn_index: 0,
    })
    expect(fork.chat_id).toBe('chat-fork')
    expect(store.activeChatId).toBe('chat-fork')
    expect(store.messages['chat-fork']).toEqual(copied)
  })
})

describe('switchChat workspace alignment', () => {
  test('automatically switches activeWorkspace to match target chat workspace', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/messages')) return Promise.resolve([])
      if (url.includes('/subagents')) return Promise.resolve([])
      return Promise.resolve([])
    })
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    store.projects = [
      { project_id: 'p-personal', name: 'Personal Proj', workspace: 'personal' } as unknown as ProjectInfo,
      { project_id: 'p-work', name: 'Work Proj', workspace: 'work' } as unknown as ProjectInfo,
    ]
    store.chats = [
      { chat_id: 'c-personal', project_id: 'p-personal', title: 'Personal Chat' } as unknown as ChatInfo,
      { chat_id: 'c-work', project_id: 'p-work', title: 'Work Chat' } as unknown as ChatInfo,
    ]

    await store.switchChat('c-work')

    expect(store.activeWorkspace).toBe('work')
    expect(store.activeChatId).toBe('c-work')
  })
})

describe('projectChatRows (delegate grouping)', () => {
  function seed(store: ReturnType<typeof useProjectStore>, chats: Partial<ChatInfo>[]) {
    store.projects = [
      { project_id: 'p1', name: 'Proj', workspace: 'personal' } as unknown as ProjectInfo,
    ]
    store.chats = chats.map((c, i) => ({
      project_id: 'p1',
      title: c.chat_id,
      archived: false,
      created_at: `2026-07-31T00:0${i}:00Z`,
      ...c,
    })) as unknown as ChatInfo[]
  }

  test('delegates follow their supervisor and are marked', () => {
    const store = useProjectStore()
    seed(store, [
      { chat_id: 'boss' },
      { chat_id: 'other' },
      { chat_id: 'd1', spawned_from_chat_id: 'boss' },
      { chat_id: 'd2', spawned_from_chat_id: 'boss' },
    ])

    const rows = store.projectChatRows('p1')

    expect(rows.map(r => r.chat.chat_id)).toEqual(['boss', 'd1', 'd2', 'other'])
    expect(rows.map(r => r.isDelegate)).toEqual([false, true, true, false])
  })

  test('returns one collapsible group for a supervisor and its delegates', () => {
    const store = useProjectStore()
    seed(store, [
      { chat_id: 'boss', title: 'Supervisor' },
      { chat_id: 'd1', title: 'First task', spawned_from_chat_id: 'boss' },
      { chat_id: 'd2', title: 'Second task', spawned_from_chat_id: 'boss' },
      { chat_id: 'other', title: 'Other' },
    ])

    const groups = store.projectChatGroups('p1')

    expect(groups.map(group => group.chat.chat_id)).toEqual(['boss', 'other'])
    expect(groups[0].delegates.map(chat => chat.chat_id)).toEqual(['d1', 'd2'])
    expect(groups[1].delegates).toEqual([])
  })

  test('an orphaned delegate stays top-level instead of disappearing', () => {
    const store = useProjectStore()
    // Supervisor archived, so it is not in the visible list at all.
    seed(store, [
      { chat_id: 'boss', archived: true },
      { chat_id: 'orphan', spawned_from_chat_id: 'boss' },
    ])

    const rows = store.projectChatRows('p1')

    expect(rows.map(r => r.chat.chat_id)).toEqual(['orphan'])
    expect(rows[0].isDelegate).toBe(false)
  })

  test('a delegate whose supervisor lives in another project is not hidden', () => {
    const store = useProjectStore()
    seed(store, [{ chat_id: 'orphan', spawned_from_chat_id: 'boss-elsewhere' }])
    store.chats = [
      ...store.chats,
      {
        chat_id: 'boss-elsewhere',
        project_id: 'p2',
        title: 'Boss',
        archived: false,
        created_at: '2026-07-31T00:00:00Z',
      } as unknown as ChatInfo,
    ]

    expect(store.projectChatRows('p1').map(r => r.chat.chat_id)).toEqual(['orphan'])
  })

  test('chats with no delegates produce a plain flat list', () => {
    const store = useProjectStore()
    seed(store, [{ chat_id: 'a' }, { chat_id: 'b' }])

    const rows = store.projectChatRows('p1')

    expect(rows.map(r => r.chat.chat_id)).toEqual(['a', 'b'])
    expect(rows.every(r => !r.isDelegate)).toBe(true)
  })
})

describe('activeChatsAll (hide nested delegates)', () => {
  function seed(store: ReturnType<typeof useProjectStore>, chats: Partial<ChatInfo>[]) {
    store.projects = [
      { project_id: 'p1', name: 'Proj', workspace: 'personal' } as unknown as ProjectInfo,
    ]
    store.chats = chats.map((c, i) => ({
      project_id: 'p1',
      title: c.title || c.chat_id,
      archived: false,
      local: true,
      created_at: `2026-07-31T00:0${i}:00Z`,
      last_activity_at: `2026-07-31T01:0${i}:00Z`,
      ...c,
    })) as unknown as ChatInfo[]
  }

  test('nested delegates are omitted from jump-back-in', () => {
    const store = useProjectStore()
    seed(store, [
      { chat_id: 'boss', title: 'Architecture Review' },
      { chat_id: 'other', title: 'Other' },
      { chat_id: 'd1', title: 'Arch review: a', spawned_from_chat_id: 'boss' },
      { chat_id: 'd2', title: 'Arch review: b', spawned_from_chat_id: 'boss' },
    ])

    expect(store.activeChatsAll.map(c => c.chat_id)).toEqual(['other', 'boss'])
  })

  test('orphaned delegates remain listed when the supervisor is gone', () => {
    const store = useProjectStore()
    seed(store, [
      { chat_id: 'boss', archived: true },
      { chat_id: 'orphan', spawned_from_chat_id: 'boss' },
    ])

    expect(store.activeChatsAll.map(c => c.chat_id)).toEqual(['orphan'])
  })
})

describe('delegate unread notifications', () => {
  test('internal delegate activity is not reported as unread', () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Proj', workspace: 'personal' } as unknown as ProjectInfo,
    ]
    store.chats = [
      {
        chat_id: 'boss',
        project_id: 'p1',
        title: 'Supervisor',
        archived: false,
        local: true,
        created_at: '2026-07-31T00:00:00Z',
        last_activity_at: '2026-07-31T01:00:00Z',
        last_read_at: '2026-07-31T01:00:00Z',
      },
      {
        chat_id: 'child',
        project_id: 'p1',
        title: 'Internal task',
        archived: false,
        local: true,
        spawned_from_chat_id: 'boss',
        created_at: '2026-07-31T00:00:00Z',
        last_activity_at: '2026-07-31T02:00:00Z',
        last_read_at: '2026-07-31T01:00:00Z',
      },
    ] as unknown as ChatInfo[]

    expect(store.chatUnread('child')).toBe(0)
  })
})

describe('workspaceNeedsInput', () => {
  test('sums only the requested workspace and returns zero when clear', () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p-personal', name: 'Personal', workspace: 'personal' },
      { project_id: 'p-work', name: 'Work', workspace: 'work' },
    ] as unknown as ProjectInfo[]
    store.chats = [
      {
        chat_id: 'c-personal', project_id: 'p-personal', title: 'Personal', archived: false, local: true,
        pending_question: JSON.stringify({ questions: [{ question: 'Answer?' }] }),
      },
      {
        chat_id: 'c-work', project_id: 'p-work', title: 'Work', archived: false, local: true,
      },
    ] as unknown as ChatInfo[]

    expect(store.workspaceNeedsInput('personal')).toBe(1)
    expect(store.workspaceNeedsInput('work')).toBe(0)
    expect(store.workspaceNeedsInput('missing')).toBe(0)
  })
})
