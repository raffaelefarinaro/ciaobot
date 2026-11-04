// @vitest-environment jsdom
//
// Opening chat B after its turn finished while the user was in chat A shows
// the user's message as the last row and no reply, until a full page reload
// (BrandMark) rebuilds the store from scratch.
//
// Every scenario is driven through the store's real entry points: switchChat,
// sendMessage, the /ws/events socket (snapshot, chat_streaming_started/done)
// and the per-chat /ws/chat/<id> socket (broker frames).
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
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
  // As far as the store cares, the handshake completes synchronously.
  set onopen(fn: (() => void) | null) { fn?.() }
  get onopen() { return null }
  constructor(public url: string) { sockets.push(this) }
  close() { this.readyState = 3 }
}
let sockets: FakeWebSocket[] = []
function frame(path: string, data: object) {
  const ws = sockets.filter(s => s.url.endsWith(path) && s.readyState === 1).at(-1)
  if (!ws) throw new Error(`no open socket for ${path}`)
  ws.onmessage?.({ data: JSON.stringify(data) })
}

const chat = (id: string) => ({
  chat_id: id, project_id: 'p1', title: id, model: '', provider: 'claude', mode: '', session_id: 's', created_at: '',
})
const snapshot = (active_streams: object[] = []) => ({
  type: 'snapshot', active_streams, background_agents: {}, chat_pins: {}, chat_terminal_offers: {},
})

// What GET /api/chats/<id>/messages returns right now, per chat.
let history: Record<string, object[]> = {}
const envelope = (items: object[]) => ({ items, total: items.length, offset: 0, limit: 50, hasMore: false, nextOffset: null })

beforeEach(() => {
  // restoreState() reads the last active chat; a leftover 'B' would make
  // switchChat('B') take its already-active early return.
  localStorage.clear()
  setActivePinia(createPinia())
  sockets = []
  history = {}
  apiGet.mockReset()
  apiGet.mockImplementation((url: string) => {
    const m = url.match(/^\/api\/chats\/([^/?]+)\/messages/)
    if (m) return Promise.resolve(envelope(history[m[1]] || []))
    if (url.startsWith('/api/chats?active_only=1')) return Promise.resolve([chat('A'), chat('B')])
    return Promise.resolve([])
  })
  vi.stubGlobal('WebSocket', FakeWebSocket)
})
afterEach(() => { vi.unstubAllGlobals() })

const PROMPT = 'Build the report'
const REPLY = 'Here is the report.'
const earlier = [
  { role: 'user', content: 'hi', i: 0, turn_index: 0, sent_at: '2026-11-04T10:00:00Z' },
  { role: 'assistant', content: 'hello', i: 1, sent_at: '2026-11-04T10:00:05Z', phase: 'final_answer' },
]
// B's turn, finished server-side: the reply is in the session and carries its
// completion overlay (`sent_at`).
const finishedTurn = [
  ...earlier,
  { role: 'user', content: PROMPT, i: 2, turn_index: 1, sent_at: '2026-11-04T10:01:00Z' },
  { role: 'system', content: 'Read report guide', tool_name: '_activity', i: 3 },
  { role: 'assistant', content: REPLY, i: 4, sent_at: '2026-11-04T10:02:00Z', phase: 'final_answer' },
]

/**
 * The user is in B, sends PROMPT, watches the first tool step, then clicks
 * chat A in the sidebar while B's turn is still running. B's turn then
 * finishes server-side and the events socket reports it.
 */
async function leaveBMidTurnAndLetItFinish() {
  const store = useProjectStore()
  store.chats = [chat('A'), chat('B')] as never
  store.connectEventsWs()
  frame('/ws/events', snapshot())
  history.A = earlier
  history.B = earlier
  await store.switchChat('B')
  store.sendMessage('B', PROMPT)
  frame('/ws/events', { type: 'chat_streaming_started', chat_id: 'B', project_id: 'p1' })
  frame('/ws/chat/B', { type: 'user_echo', text: PROMPT, turn_index: 1 })
  frame('/ws/chat/B', { type: 'tool_use', tool_name: 'Read', tool_input: '{}', description: 'Read report guide' })
  await store.switchChat('A')
  history.B = finishedTurn
  frame('/ws/events', { type: 'chat_streaming_done', chat_id: 'B', project_id: 'p1' })
  return store
}

const hasReply = (store: ReturnType<typeof useProjectStore>) =>
  (store.messages.B || []).some(m => m.role === 'assistant' && m.content === REPLY)

// The engine starts a turn of its own in B while the user is still in A: a
// background shell (or CLI task) finished and `_deliver_wake` ran
// `start_stream(origin="cli_task_wake")`; a queued follow-up drained by the
// server is the same shape with a plain user row. Both are ordinary
// (non-background) streams, so the events socket says chat_streaming_started
// without `background`. The user then clicks B. `/messages` answers before the
// broker's replayed user_echo for the engine turn reaches the store.
const engineTurns = [
  ['cli_task_wake turn', { role: 'user', content: 'Background task finished: report.sh', origin: 'cli_task_wake' }],
  ['queued follow-up drained by the server', { role: 'user', content: 'And email it to me' }],
] as const
for (const [label, engineRow] of engineTurns) {
  test(`reopening B while a ${label} runs keeps the finished reply of the turn before it`, async () => {
    const store = await leaveBMidTurnAndLetItFinish()
    frame('/ws/events', { type: 'chat_streaming_started', chat_id: 'B', project_id: 'p1' })
    history.B = [
      ...finishedTurn,
      { ...engineRow, i: 5, turn_index: 2, sent_at: '2026-11-04T10:05:00Z' },
      { role: 'system', content: 'Read report guide and locate html-artifact skill', tool_name: '_activity', i: 6 },
    ]

    await store.switchChat('B')
    // The broker replay of the running engine turn lands after history.
    frame('/ws/chat/B', {
      type: 'user_echo', text: engineRow.content, turn_index: 2,
      ...('origin' in engineRow ? { origin: engineRow.origin } : {}),
    })
    frame('/ws/chat/B', {
      type: 'tool_use', tool_name: 'Read', tool_input: '{}',
      description: 'Read report guide and locate html-artifact skill',
    })

    // Screenshot 1: "Working · Read report guide and locate html-artifact
    // skill", composer "Reply — it is queued until Ciao finishes" ...
    expect(store.isChatStreaming('B')).toBe(true)
    // ... and the reply to PROMPT, which the server holds, is gone.
    expect(hasReply(store)).toBe(true)
    // The engine turn's own progress stays out of history: the live trace owns it.
    expect((store.messages.B || []).some(m => m.tool_name === '_activity' && m.content.includes('html-artifact'))).toBe(false)
  })
}

// The engine turn above ends while the events socket is between connections
// (the desktop window was hidden; resumeActiveChat closes the events socket on
// visibilitychange), so the client never sees chat_streaming_done for B, and
// the broker stream had finished before B's chat socket attached, so nothing
// was replayed and local `streaming.B` is false. The fresh snapshot no longer
// lists B. Screenshot 2: no spinner, composer "Reply to Ciao", last row the
// user's message.
test('a snapshot that ends the stream of the open chat refetches the history it truncated', async () => {
  const store = await leaveBMidTurnAndLetItFinish()
  frame('/ws/events', { type: 'chat_streaming_started', chat_id: 'B', project_id: 'p1' })
  history.B = [
    ...finishedTurn,
    { role: 'user', content: 'Background task finished: report.sh', origin: 'cli_task_wake', i: 5, turn_index: 2, sent_at: '2026-11-04T10:05:00Z' },
    { role: 'system', content: 'Read x', tool_name: '_activity', i: 6 },
  ]
  await store.switchChat('B')

  history.B = [
    ...history.B,
    { role: 'assistant', content: 'Report sent.', i: 7, sent_at: '2026-11-04T10:06:00Z', phase: 'final_answer' },
  ]
  frame('/ws/events', snapshot([]))

  expect(store.isChatStreaming('B')).toBe(false)
  // Nothing refetches B now; only the next 15 s syncLatest tick or a reload.
  await vi.waitFor(() => expect(hasReply(store)).toBe(true), { timeout: 1000 })
})
