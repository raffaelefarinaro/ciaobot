// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { enableAutoUnmount, flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

const PaneHeaderStub = defineComponent({
  name: 'PaneHeaderStub',
  setup(_, { slots }) {
    return () => h('header', { class: 'pane-header' }, [
      slots.title?.(),
      h('div', { class: 'header-actions' }, slots.actions?.()),
    ])
  },
})

const ChildStub = defineComponent({
  name: 'ChildStub',
  setup() {
    return () => h('div')
  },
})

const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({
      openId: null,
      close: vi.fn(),
      clearPendingClose: vi.fn(),
      onTargetOver: vi.fn(),
      onTargetOut: vi.fn(),
      pinFromEvent: vi.fn(),
      show: vi.fn(),
    })
    return () => h('div')
  },
})

const MODELS_RESPONSE = {
  models: ['haiku', 'sonnet', 'opus', 'fable'],
  default: 'sonnet',
  provider_models: { claude: ['haiku', 'sonnet', 'opus', 'fable'] },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

function makeProject(): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'General',
    workspace: 'personal',
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
  }
}

function makeChat(chatId: string, title = chatId): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title,
    model: 'sonnet',
    provider: 'opencode',
    mode: 'normal',
    session_id: '',
    created_at: '',
    archived: false,
  }
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

// Every panel this file mounts is torn down when its test ends. A panel that
// outlives its test keeps its scroll-settle loop alive, and that loop calls
// `store.loadOlderMessages()` on a requestAnimationFrame. Store actions reset
// the one global "active pinia" the next `useProjectStore()` reads. A leftover
// panel could therefore hand the next test the previous test's store.
enableAutoUnmount(afterEach)

/**
 * Mounts the panel on a pinia of its own and hands back that pinia's store.
 *
 * The store comes back with the wrapper on purpose: resolving it again inside
 * a test would read the global active pinia, which a panel from an earlier
 * test can replace (see the note above). Mutating a store the panel does not
 * use is silent — the assertions then read a panel that never saw the
 * change — and it is exactly what made this file fail on a loaded machine.
 */
async function mountPanel(): Promise<{ wrapper: VueWrapper; store: ReturnType<typeof useProjectStore> }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  // The second chat is one the transcript may name: a reference to it links
  // to the conversation under its human title (#1768).
  store.chats = [makeChat('chat-1'), makeChat('chat-0123abcd', 'Budget review')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.bootstrapped = true

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

// Issue #867: the transcript renders message content with v-html through the
// shared markdown pipeline, so text the person typed came back out as markup —
// `<p id=out>` vanished from the bubble and the line split there. A user bubble
// has to read as typed; the assistant bubble keeps the sanitized passthrough.
describe('ChatPanel user message rendering', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('shows typed raw HTML in a user bubble and not in the assistant one', async () => {
    const typed = '<p id=out> <img src=x onerror=alert(1)>'
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: typed, timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      { role: 'assistant', content: typed, timestamp: '2026-08-30T13:23:10Z' },
    ]
    await flushPromises()

    const user = wrapper.get('.message-wrap.user .message-content')
    expect(user.text()).toContain('<p id=out>')
    expect(user.text()).toContain('<img src=x onerror=alert(1)>')
    expect(user.find('img').exists()).toBe(false)
    expect(user.find('#out').exists()).toBe(false)

    // The assistant path is unchanged: sanitized markup, handler stripped.
    const assistant = wrapper.get('.message-wrap.assistant .message-content')
    expect(assistant.find('#out').exists()).toBe(true)
    const img = assistant.find('img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('onerror')).toBeUndefined()
  })
})

// Issue #1768: a message that names another conversation links to it, under
// its human title. The link is the app's own: it opens in this tab, and the
// raw id stays in the tooltip and in `data-chat-id`.
describe('ChatPanel chat references', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('links a known conversation in a user bubble and in an assistant one', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'look at chat-0123abcd please', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      { role: 'assistant', content: 'done, see chat-0123abcd', timestamp: '2026-08-30T13:23:10Z' },
    ]
    await flushPromises()

    for (const selector of [
      '.message-wrap.user .message-content',
      '.message-wrap.assistant .message-content',
    ]) {
      const link = wrapper.get(`${selector} a.chat-link`)
      expect(link.text(), selector).toBe('Budget review')
      expect(link.attributes('href'), selector).toBe('/chat/chat-0123abcd')
      expect(link.attributes('title'), selector).toBe('chat-0123abcd')
      expect(link.attributes('data-chat-id'), selector).toBe('chat-0123abcd')
      // A link into the app opens in this tab, not in a second copy of it.
      expect(link.attributes('target'), selector).toBeUndefined()
    }
  })

  it('leaves an unknown conversation plain', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'look at chat-ffffffff please', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
    ]
    await flushPromises()

    const user = wrapper.get('.message-wrap.user .message-content')
    expect(user.find('a.chat-link').exists()).toBe(false)
    expect(user.text()).toContain('chat-ffffffff')
  })

  it('shows the new title when the conversation is renamed', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'assistant', content: 'done, see chat-0123abcd', timestamp: '2026-08-30T13:23:10Z' },
    ]
    await flushPromises()
    expect(wrapper.get('.message-wrap.assistant .message-content a.chat-link').text()).toBe('Budget review')

    // The rendered message is cached by its text, so a renamed conversation
    // has to drop that cache: the label follows the title.
    const chat = store.chats.find(item => item.chat_id === 'chat-0123abcd')!
    chat.title = 'Budget review, final'
    await flushPromises()
    expect(wrapper.get('.message-wrap.assistant .message-content a.chat-link').text()).toBe('Budget review, final')
  })

  it('only links chats offered by the openable-chat list', async () => {
    const { wrapper, store } = await mountPanel()
    store.projects = []
    store.messages['chat-1'] = [
      { role: 'assistant', content: 'see chat-0123abcd', timestamp: '2026-08-30T13:23:10Z' },
    ]
    await flushPromises()

    expect(wrapper.find('.message-wrap.assistant .message-content a.chat-link').exists()).toBe(false)
  })

  it('keeps the chat-link map instance when only activity time changes', async () => {
    const { wrapper, store } = await mountPanel()
    const setupState = (wrapper.vm as unknown as {
      $: { setupState: { knownChatLinks: Map<string, string> } }
    }).$.setupState
    const originalMap = setupState.knownChatLinks
    const linkedChat = store.chats.find(item => item.chat_id === 'chat-0123abcd')!

    linkedChat.last_activity_at = '2026-10-22T12:00:00Z'
    await flushPromises()

    expect(setupState.knownChatLinks).toBe(originalMap)
  })
})
