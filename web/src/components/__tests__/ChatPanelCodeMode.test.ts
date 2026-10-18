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

async function mountPanel(): Promise<{ wrapper: VueWrapper }> {
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
      },
    },
  })
  await flushPromises()
  return { wrapper }
}

describe('ChatPanel code mode rows', () => {
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

  const turns = () => [
    { role: 'user' as const, content: 'first', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
    { role: 'assistant' as const, content: 'one', timestamp: '2026-08-30T13:23:10Z' },
    { role: 'user' as const, content: 'second', timestamp: '2026-08-30T13:24:00Z', turn_index: 1 },
    { role: 'assistant' as const, content: 'two', timestamp: '2026-08-30T13:24:10Z' },
  ]

  it('renders a switch row between turns', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.chats[0].code_mode_events = [
      { code_mode: true, actor: 'agent', at: '2026-08-30T13:23:30Z', after_turn_index: 0 },
    ]
    store.messages['chat-1'] = turns()
    await flushPromises()

    const row = wrapper.get('.transcript-event')
    expect(row.text()).toContain('Switched to code mode · by Ciao')
    expect(wrapper.find('.message.system').exists()).toBe(false)

    const order = Array.from((wrapper.element as HTMLElement).querySelectorAll<HTMLElement>(
      '.message-wrap.assistant, .message-wrap.user, .transcript-event',
    )).map((el) => (el.classList.contains('transcript-event') ? 'event' : el.classList.contains('user') ? 'user' : 'assistant'))
    expect(order).toEqual(['user', 'assistant', 'event', 'user', 'assistant'])
  })

  it("shows a new switch when the chat's events change", async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.messages['chat-1'] = turns()
    store.chats[0].code_mode_events = []
    await flushPromises()
    expect(wrapper.find('.transcript-event').exists()).toBe(false)

    store.chats[0].code_mode_events = [
      { code_mode: false, actor: 'user', at: '2026-08-30T13:25:00Z', after_turn_index: 1 },
    ]
    await flushPromises()
    expect(wrapper.get('.transcript-event').text()).toContain('Left code mode · by you')
  })
})

describe('ChatPanel code badge', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo
  const REPO = { path: '/r/app', branch: 'main', dirty: false, missing: false, instructions: null }

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    document.body.innerHTML = ''
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('shows a code badge beside the title only while code mode is on', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.projects[0].repo = REPO
    store.chats[0].code_mode = true
    await flushPromises()
    expect(wrapper.get('.chat-title-row .chat-code-badge').text()).toBe('code')
    expect(wrapper.find('.header-actions .repo-chip').exists()).toBe(false)

    store.chats[0].code_mode = false
    await flushPromises()
    expect(wrapper.find('.chat-code-badge').exists()).toBe(false)
    expect(wrapper.find('.header-actions .repo-chip').exists()).toBe(false)
  })

  it('shows no badge for an unlinked chat in code mode', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.chats[0].code_mode = true
    await flushPromises()
    expect(wrapper.find('.chat-code-badge').exists()).toBe(false)
  })

  it('opens Chat details at the Repository section from the badge', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.projects[0].repo = REPO
    store.chats[0].code_mode = true
    await flushPromises()
    await wrapper.get('.chat-code-badge').trigger('click')
    await flushPromises()
    const rail = document.querySelector('#chat-work-rail') ?? wrapper.element.querySelector('#chat-work-rail')
    expect(rail).not.toBeNull()
    expect(rail!.querySelector('.chat-rail-repo')).not.toBeNull()
  })
})
