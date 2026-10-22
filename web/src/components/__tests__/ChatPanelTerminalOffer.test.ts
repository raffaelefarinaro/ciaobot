// @vitest-environment jsdom

/**
 * The suggested-command card, where it sits in a chat.
 *
 * The card is contextual: it belongs to one chat, it appears above the
 * composer's queue so a command waiting to be reviewed is never buried under
 * the transcript, and it is not shown for a chat that is not on screen.
 *
 * A note on switching chats: production does not reuse one panel across two
 * chats. `ChatLayout.vue` renders `<ChatPanel :key="store.activeChat.chat_id">`,
 * so a new chat mounts a new panel and the old one is torn down. These tests
 * therefore never flip `activeChatId` on a mounted panel — that state is not
 * what the app does, and a card's own state (a conflict notice, a copy
 * confirmation) cannot survive a remount, because the component is new. The
 * remount is driven directly where that matters.
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

describe('ChatPanel suggested command', () => {
  it('shows the chat\'s own offer above the queue and the composer', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.terminalOffers = { [CHAT_ID]: { id: 'offer-1', command: 'ciao service start' } }
    await flushPromises()

    const card = panel.find('.terminal-offer')
    expect(card.exists()).toBe(true)
    // The command as it is on screen, byte for byte: `textContent`, not
    // VTU's `text()`, which trims.
    expect(card.get('.terminal-offer-command code').element.textContent).toBe('ciao service start')

    // Above the queue and above the composer: a command waiting to be
    // reviewed is never buried under the transcript.
    const composer = panel.get('.chat-input').element
    expect(comesBefore(card.element, composer)).toBe(true)

    store.queuedMessages[CHAT_ID] = [{ id: 'q1', text: 'one', delivery: 'queue', steering: false }] as never
    await flushPromises()
    const queue = panel.get('.queued-messages').element
    expect(comesBefore(card.element, queue)).toBe(true)
  })

  it('shows exactly one card, after the permission prompts', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.pendingPermissions[CHAT_ID] = [{
      request_id: 'req-1',
      tool_name: 'Bash',
      tool_input: '',
      message: '',
      received_at: 1,
    }]
    store.terminalOffers = { [CHAT_ID]: { id: 'offer-1', command: 'ciao service start' } }
    await flushPromises()

    expect(panel.findAll('.terminal-offer')).toHaveLength(1)
    // The card is contextual, and it sits below the prompts that block a
    // turn: the permission is the thing the user has to answer first.
    const permissions = panel.get('.permission-requests').element
    const card = panel.get('.terminal-offer').element
    expect(comesBefore(permissions, card)).toBe(true)
  })

  it('renders the map\'s offer, not the row\'s copy', async () => {
    const { wrapper: panel, store } = await mountPanel()
    // The row carries the offer as it was read; the map carries the live one.
    store.chats = [makeChat({ terminal_offer: { id: 'old', command: 'the old command' } })]
    store.terminalOffers = { [CHAT_ID]: { id: 'new', command: 'the new command' } }
    await flushPromises()

    expect(panel.get('.terminal-offer-command code').element.textContent).toBe('the new command')
  })

  it('ignores another chat\'s offer', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.terminalOffers = { 'other-chat': { id: 'offer-9', command: 'rm -rf /' } }
    await flushPromises()

    expect(panel.find('.terminal-offer').exists()).toBe(false)
  })

  it('shows no card for a chat that holds no offer', async () => {
    const { wrapper: panel } = await mountPanel()
    expect(panel.find('.terminal-offer').exists()).toBe(false)
  })

  it('hides the card for an archived chat', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.chats = [makeChat({ archived: true })]
    store.terminalOffers = { [CHAT_ID]: { id: 'offer-1', command: 'ciao service start' } }
    await flushPromises()

    expect(panel.find('.terminal-offer').exists()).toBe(false)
  })

  it('hands the focus to the composer when the card is dismissed', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.terminalOffers = { [CHAT_ID]: { id: 'offer-1', command: 'ciao service start' } }
    await flushPromises()

    const dismiss = panel.get('.terminal-offer-dismiss')
    ;(dismiss.element as HTMLElement).focus()
    expect(document.activeElement).toBe(dismiss.element)

    // The offer goes: the card unmounts, and the focus must not fall to the
    // page — the composer is where the next word goes.
    store.terminalOffers = {}
    await flushPromises()
    await nextTick()

    expect(panel.find('.terminal-offer').exists()).toBe(false)
    expect(document.activeElement).toBe(panel.get('.chat-input').element)
  })

  it('hands the focus to the composer when the engine drops the offer', async () => {
    const { wrapper: panel, store } = await mountPanel()
    store.terminalOffers = { [CHAT_ID]: { id: 'offer-1', command: 'ciao service start' } }
    await flushPromises()

    const dismiss = panel.get('.terminal-offer-dismiss')
    ;(dismiss.element as HTMLElement).focus()

    // The engine drops the offer over the socket: the map loses the entry
    // and the card goes with it.
    delete store.terminalOffers[CHAT_ID]
    await flushPromises()
    await nextTick()

    expect(panel.find('.terminal-offer').exists()).toBe(false)
    expect(document.activeElement).toBe(panel.get('.chat-input').element)
  })

  it('hands the focus to the composer bar, not the textarea, on a narrow pane', async () => {
    const width = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 375, configurable: true, writable: true })
    try {
      const { wrapper: panel, store } = await mountPanel()
      store.terminalOffers = { [CHAT_ID]: { id: 'offer-1', command: 'ciao service start' } }
      await flushPromises()

      const dismiss = panel.get('.terminal-offer-dismiss')
      ;(dismiss.element as HTMLElement).focus()

      store.terminalOffers = {}
      await flushPromises()
      await nextTick()

      expect(panel.find('.terminal-offer').exists()).toBe(false)
      // The textarea would raise the soft keyboard and scroll the transcript
      // out from under the reader, so the bar takes the focus there.
      expect(document.activeElement).toBe(panel.get('.composer-bar').element)
      expect(document.activeElement).not.toBe(panel.get('.chat-input').element)
    } finally {
      Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true })
    }
  })

  it('carries no state into the next chat, because the panel is remounted', async () => {
    // Production keys the panel by chat id (ChatLayout.vue), so a new chat
    // mounts a new panel over the same store. The remount is driven here
    // rather than by flipping `activeChatId` on a mounted panel, which is not
    // what the app does.
    const first = await mountPanel('chat-a')
    first.store.terminalOffers = { 'chat-a': { id: 'offer-a', command: 'ciao service start' } }
    await flushPromises()

    // A conflict notice and a copy confirmation on chat A.
    apiPatch.mockRejectedValueOnce({
      status: 409,
      payload: {
        error: 'terminal_offer_conflict',
        terminal_offer: { id: 'offer-a2', command: 'ciao service start' },
      },
    })
    await first.wrapper.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()
    expect(first.wrapper.find('.terminal-offer-notice').exists()).toBe(true)

    await first.wrapper.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(first.wrapper.get('.terminal-offer-copy').text()).toBe('Copied')

    // The panel is remounted for the next chat, over the same store.
    first.wrapper.unmount()
    wrapper = null
    const second = await mountPanel('chat-b', first.pinia)
    second.store.terminalOffers = { 'chat-b': { id: 'offer-b', command: 'ciao service start' } }
    await flushPromises()

    const card = second.wrapper.get('.terminal-offer')
    expect(card.find('.terminal-offer-notice').exists()).toBe(false)
    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
  })
})
