// @vitest-environment jsdom

/**
 * The needs-you card (an MCP sign-in, ...), where it sits in a chat.
 *
 * Like the suggested command, the card is contextual: it belongs to one chat,
 * sits above the composer's queue, and is not shown for a chat that is not on
 * screen. Setup is the same as the suggested-command test, so the two cards
 * are mounted the same way.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import type { ChatInfo } from '../../lib/types'

// The store's onMounted fetches (models, commands, loops, schedules) all
// swallow errors, so a rejecting api keeps the mount path quiet while the
// card is driven purely from store state.
const apiGet = vi.hoisted(() => vi.fn(() => Promise.reject(new Error('no server'))))
const apiPost = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiDel = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const writeClipboard = vi.hoisted(() => vi.fn(async (_text: string) => true))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel, postForm: vi.fn() },
}))

vi.mock('../../lib/codeCopy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/codeCopy')>()
  return { ...actual, writeClipboard }
})

vi.mock('../../router', () => ({
  router: {
    push: vi.fn(),
    currentRoute: { value: { params: {} } },
  },
}))

// Heavy or irrelevant children are stubbed at the module level: Vue SFCs
// import siblings directly through ESM, which bypasses `config.global.stubs`.
const NoopStub = vi.hoisted(() => ({ name: 'NoopStub', render: () => null }))
const CommentPopoverStub = vi.hoisted(() => ({
  name: 'CommentPopoverStub',
  render: () => null,
  methods: {
    close: () => {},
    clearPendingClose: () => {},
    onTargetOver: () => {},
    onTargetOut: () => {},
    pinFromEvent: () => null,
    show: () => {},
  },
}))
vi.mock('../PaneHeader.vue', () => ({ default: NoopStub }))
vi.mock('../SubagentPanel.vue', () => ({ default: NoopStub }))
vi.mock('../ChatCommentPopover.vue', () => ({ default: CommentPopoverStub }))
vi.mock('../CommentComposePopover.vue', () => ({ default: NoopStub }))

import ChatPanel from '../ChatPanel.vue'

const CHAT_ID = 'chat-1'

function makeChat(overrides: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: CHAT_ID,
    project_id: 'proj-1',
    title: 'a chat',
    model: 'synthetic-model',
    provider: 'claude',
    mode: 'normal',
    session_id: 'sess-1',
    created_at: '2026-08-01T00:00:00Z',
    archived: false,
    ...overrides,
  } as ChatInfo
}

let wrapper: VueWrapper | null = null

async function mountPanel(chatId = CHAT_ID, existing?: ReturnType<typeof createPinia>) {
  // One store for a remount: production keys the panel by chat id, so the
  // store outlives the panel and the new one sees the old one's state.
  const pinia = existing ?? createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.chats = [makeChat({ chat_id: chatId })]
  store.activeChatId = chatId
  store.messages = { [chatId]: [] }
  store.bootstrapped = true
  // Attached to the document: the card hands the focus to the composer when
  // it goes, and focus only exists in a document.
  wrapper = mount(ChatPanel, {
      props: { chatId: chatId },
    attachTo: document.body,
    global: { plugins: [pinia], stubs: { RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return { wrapper, store, pinia }
}

/** True when `first` comes before `second` in the document. */
function comesBefore(first: Element, second: Element): boolean {
  return Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)
}

beforeEach(() => {
  vi.clearAllMocks()
  writeClipboard.mockReset().mockResolvedValue(true)
  apiPatch.mockReset().mockResolvedValue({})
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

const ACTION = {
  id: 'a1',
  kind: 'mcp_auth',
  origin: 'engine',
  state: 'open',
  params: { server: 'linear', provider: 'opencode' },
  error: '',
  created_at: 1,
} as const

describe('ChatPanel needs-you card', () => {
  it('shows the chat\'s needs-you card above the queue and the composer', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.chatActions = { [CHAT_ID]: [ACTION as never] }
    await flushPromises()

    const card = panel.get('.needs-you-card').element
    expect(panel.get('.needs-you-card-title').text()).toBe('Sign in to linear')

    const composer = panel.get('.chat-input').element
    expect(comesBefore(card, composer)).toBe(true)

    store.queuedMessages[CHAT_ID] = [{ id: 'q1', text: 'one', delivery: 'queue', steering: false }] as never
    await flushPromises()
    const queue = panel.get('.queued-messages').element
    expect(comesBefore(card, queue)).toBe(true)
  })

  it('shows no card for an archived chat', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.chats = [makeChat({ archived: true })]
    store.chatActions = { [CHAT_ID]: [ACTION as never] }
    await flushPromises()

    expect(panel.find('.needs-you-card').exists()).toBe(false)
  })

  it('shows no card for a kind that has no copy yet', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.chatActions = { [CHAT_ID]: [{ ...ACTION, kind: 'not_a_kind' } as never] }
    await flushPromises()

    expect(panel.find('.needs-you-card').exists()).toBe(false)
  })

  it('hands the focus to the composer when the card goes', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.chatActions = { [CHAT_ID]: [ACTION as never] }
    await flushPromises()

    const dismiss = panel.get('.needs-you-card .needs-you-btn--quiet')
    ;(dismiss.element as HTMLElement).focus()
    expect(document.activeElement).toBe(dismiss.element)

    // The action goes: the card unmounts, and the focus must not fall to the
    // page. The composer is where the next word goes.
    store.chatActions = {}
    await flushPromises()
    await nextTick()

    expect(panel.find('.needs-you-card').exists()).toBe(false)
    expect(document.activeElement).toBe(panel.get('.chat-input').element)
  })
})
