// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { shouldReconnectActiveChatOnStreamingStarted, chatWsReconnectDelayMs, useProjectStore } from './projects'

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
  reloadWhenServerReady.mockClear()
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

  test('does not render allowed rate limit status as a system message', () => {
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

    const msgs = store.messages[chatId] || []
    expect(msgs.some(m => m.role === 'system' && m.content.includes('Rate limit: allowed (five_hour)'))).toBe(false)
    expect(msgs.some(m => m.role === 'system' && m.content.includes('Rate limit: allowed_warning'))).toBe(true)
  })
})

describe('queued message replay handling', () => {
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
    expect(store.bootstrapped).toBe(true)
  })

  test('restoreState runs at store init so active chat is known before fetchAll', () => {
    window.localStorage.setItem('ciao-active-chat', 'saved-chat')
    const store = useProjectStore()
    expect(store.activeChatId).toBe('saved-chat')
    expect(store.bootstrapped).toBe(false)
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
    const sent = fakeSockets.flatMap(s => (s.send as any).mock.calls.map((c: any[]) => String(c[0])))
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

describe('provider sub-chats', () => {
  test('loads provider sub-chats and events', async () => {
    const store = useProjectStore()
    const chatId = 'parent-chat-1'
    const subchatId = 'sub-chat-1'

    const records = [{ subchat_id: subchatId, parent_chat_id: chatId, status: 'created' }]
    const events = [{ type: 'message', role: 'owner', content: 'test' }]

    apiGet.mockImplementation((path: string) => {
      if (path === `/api/chats/${chatId}/provider-subchats`) {
        return Promise.resolve(records)
      }
      if (path === `/api/provider-subchats/${subchatId}/events`) {
        return Promise.resolve(events)
      }
      return Promise.resolve([])
    })

    await store.loadProviderSubchats(chatId)
    expect(store.providerSubchats[chatId]).toEqual(records)

    await store.loadProviderSubchatEvents(subchatId)
    expect(store.providerSubchatEvents[subchatId]).toEqual(events)
  })
})
