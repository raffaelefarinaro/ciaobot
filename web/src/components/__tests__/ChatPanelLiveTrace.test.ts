// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
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

function makeChat(chatId: string): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
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

async function mountPanel(): Promise<{
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1')]
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
        ChatTurnActivity: false,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

const MID = 'Mid-turn update that is long enough to once have been its own bubble. '.repeat(4)

function turnMessages() {
  return [
    { role: 'user' as const, content: 'do it', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
    { role: 'assistant' as const, content: MID, timestamp: '2026-08-30T13:23:05Z' },
    { role: 'system' as const, content: '· read a.md', timestamp: '2026-08-30T13:23:06Z', tool_name: '_activity' },
    { role: 'assistant' as const, content: 'All done, final reply.', timestamp: '2026-08-30T13:23:10Z' },
  ]
}

function stubMetrics(el: Element, m: { scrollHeight: number; clientHeight: number; scrollTop: number }) {
  for (const [k, v] of Object.entries(m)) {
    Object.defineProperty(el, k, { configurable: true, writable: true, value: v })
  }
}

describe('ChatPanel live trace', () => {
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

  it('opens the live trace by default while streaming', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()
    expect(wrapper.get('.trace-block.live').classes()).toContain('open')
  })

  it('reopens the live trace for the next turn after the user closed it', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()
    await wrapper.get('.trace-block.live .trace-summary').trigger('click')
    expect(wrapper.get('.trace-block.live').classes()).not.toContain('open')

    store.streaming['chat-1'] = false
    await flushPromises()
    store.streaming['chat-1'] = true
    await flushPromises()
    expect(wrapper.get('.trace-block.live').classes()).toContain('open')
  })

  it('renders the finished turn collapsed with only the final reply as a bubble', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()
    store.messages['chat-1'] = turnMessages()
    store.streaming['chat-1'] = false
    await flushPromises()

    const bubbles = wrapper.findAll('.message.assistant')
    expect(bubbles.length).toBe(1)
    expect(bubbles[0].text()).toContain('All done, final reply.')
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  it('keeps the finished trace open when the user was reading, and collapses it on return to bottom', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()

    const messagesEl = wrapper.get('.messages').element
    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 0 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    store.messages['chat-1'] = turnMessages()
    store.streaming['chat-1'] = false
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).toContain('open')

    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })
})
