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
import InjectedPromptCard from '../InjectedPromptCard.vue'

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
    provider: 'claude',
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
        InjectedPromptCard: false,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

const NOTICE = [
  '[Ciaobot] 1 background run finished.',
  '',
  '\u2014 Run probe A in background (be0mbj0cr, FAILED, exit 3)',
  'log: None',
  '(no output)',
].join('\n')

describe('ChatPanel native background notice', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('draws one collapsed card between the assistant rows, not a system bubble', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'start', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      { role: 'assistant', content: 'started it', timestamp: '2026-08-30T13:23:10Z' },
      { role: 'system', content: NOTICE, origin: 'background_run', timestamp: '' },
      { role: 'assistant', content: 'it failed', timestamp: '2026-08-30T13:24:10Z' },
    ]
    await flushPromises()
    const cards = wrapper.findAllComponents(InjectedPromptCard)
    expect(cards).toHaveLength(1)
    expect(cards[0].props('raw')).toBe('')
    expect(wrapper.findAll('.injected-card')).toHaveLength(1)
    expect(wrapper.find('.injected-card-body').exists()).toBe(false)
    expect(wrapper.find('.injected-card-raw-toggle').exists()).toBe(false)
    expect(wrapper.findAll('.message.system')).toHaveLength(0)
    const html = wrapper.html()
    expect(html.indexOf('started it')).toBeLessThan(html.indexOf('injected-card'))
    expect(html.indexOf('injected-card')).toBeLessThan(html.indexOf('it failed'))
  })

  it('still draws a plain system row without origin as a system bubble', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'start', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      { role: 'system', content: NOTICE, timestamp: '' },
    ]
    await flushPromises()
    expect(wrapper.findAll('.injected-card')).toHaveLength(0)
    expect(wrapper.findAll('.message.system')).toHaveLength(1)
  })
})
